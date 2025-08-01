# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the vLLM project
import asyncio
import time
from collections.abc import AsyncGenerator, Mapping
from typing import Any, Optional, Union

from fastapi import Request

from vllm.config import ModelConfig
from vllm.engine.protocol import EngineClient
from vllm.entrypoints.logger import RequestLogger
from vllm.entrypoints.openai.protocol import (ErrorResponse, RerankDocument,
                                              RerankRequest, RerankResponse,
                                              RerankResult, RerankUsage,
                                              ScoreRequest, ScoreResponse,
                                              ScoreResponseData, UsageInfo)
from vllm.entrypoints.openai.serving_engine import OpenAIServing
from vllm.entrypoints.openai.serving_models import OpenAIServingModels
from vllm.entrypoints.score_utils import (ScoreContentPartParam,
                                          ScoreMultiModalParam,
                                          _cosine_similarity,
                                          _validate_score_input_lens,
                                          get_score_prompt)
from vllm.entrypoints.utils import _validate_truncation_size
from vllm.inputs.data import TokensPrompt
from vllm.logger import init_logger
from vllm.lora.request import LoRARequest
from vllm.outputs import PoolingRequestOutput, ScoringRequestOutput
from vllm.transformers_utils.tokenizer import AnyTokenizer, MistralTokenizer
from vllm.utils import make_async, merge_async_iterators

logger = init_logger(__name__)


class ServingScores(OpenAIServing):

    def __init__(
        self,
        engine_client: EngineClient,
        model_config: ModelConfig,
        models: OpenAIServingModels,
        *,
        request_logger: Optional[RequestLogger],
    ) -> None:
        super().__init__(engine_client=engine_client,
                         model_config=model_config,
                         models=models,
                         request_logger=request_logger)

    async def _embedding_score(
        self,
        tokenizer: AnyTokenizer,
        texts_1: list[str],
        texts_2: list[str],
        request: Union[RerankRequest, ScoreRequest],
        request_id: str,
        tokenization_kwargs: Optional[dict[str, Any]] = None,
        lora_request: Optional[Union[LoRARequest, None]] = None,
        trace_headers: Optional[Mapping[str, str]] = None,
    ) -> Union[list[PoolingRequestOutput], ErrorResponse]:
        input_texts = texts_1 + texts_2

        engine_prompts: list[TokensPrompt] = []
        tokenize_async = make_async(tokenizer.__call__,
                                    executor=self._tokenizer_executor)

        tokenization_kwargs = tokenization_kwargs or {}
        tokenized_prompts = await asyncio.gather(
            *(tokenize_async(t, **tokenization_kwargs) for t in input_texts))

        for tok_result, input_text in zip(tokenized_prompts, input_texts):

            text_token_prompt = \
                self._validate_input(
                    request,
                    tok_result["input_ids"],
                    input_text)

            engine_prompts.append(
                TokensPrompt(
                    prompt_token_ids=text_token_prompt["prompt_token_ids"]))

        # Schedule the request and get the result generator.
        generators: list[AsyncGenerator[PoolingRequestOutput, None]] = []
        pooling_params = request.to_pooling_params()

        try:
            pooling_params.verify("embed", self.model_config)
        except ValueError as e:
            return self.create_error_response(str(e))

        for i, engine_prompt in enumerate(engine_prompts):

            request_id_item = f"{request_id}-{i}"

            self._log_inputs(request_id_item,
                             input_texts[i],
                             params=pooling_params,
                             lora_request=lora_request)

            generators.append(
                self.engine_client.encode(
                    engine_prompt,
                    pooling_params,
                    request_id_item,
                    lora_request=lora_request,
                    trace_headers=trace_headers,
                    priority=request.priority,
                ))

        result_generator = merge_async_iterators(*generators)

        # Non-streaming response
        final_res_batch: list[PoolingRequestOutput] = []

        embeddings: list[Optional[PoolingRequestOutput]] =\
              [None] * len(engine_prompts)

        async for i, res in result_generator:
            embeddings[i] = res

        emb_texts_1: list[PoolingRequestOutput] = []
        emb_texts_2: list[PoolingRequestOutput] = []

        for i in range(0, len(texts_1)):
            assert (emb := embeddings[i]) is not None
            emb_texts_1.append(emb)

        for i in range(len(texts_1), len(embeddings)):
            assert (emb := embeddings[i]) is not None
            emb_texts_2.append(emb)

        if len(emb_texts_1) == 1:
            emb_texts_1 = emb_texts_1 * len(emb_texts_2)

        final_res_batch = _cosine_similarity(tokenizer=tokenizer,
                                             embed_1=emb_texts_1,
                                             embed_2=emb_texts_2)

        return final_res_batch

    def _preprocess_score(
        self,
        request: Union[RerankRequest, ScoreRequest],
        tokenizer: AnyTokenizer,
        tokenization_kwargs: dict[str, Any],
        data_1: Union[str, ScoreContentPartParam],
        data_2: Union[str, ScoreContentPartParam],
    ) -> tuple[str, TokensPrompt]:

        model_config = self.model_config

        full_prompt, engine_prompt = get_score_prompt(
            model_config=model_config,
            data_1=data_1,
            data_2=data_2,
            tokenizer=tokenizer,
            tokenization_kwargs=tokenization_kwargs,
        )
        if request.mm_processor_kwargs is not None:
            engine_prompt["mm_processor_kwargs"] = request.mm_processor_kwargs

        return full_prompt, engine_prompt

    async def _cross_encoding_score(
        self,
        tokenizer: AnyTokenizer,
        data_1: Union[list[str], list[ScoreContentPartParam]],
        data_2: Union[list[str], list[ScoreContentPartParam]],
        request: Union[RerankRequest, ScoreRequest],
        request_id: str,
        tokenization_kwargs: Optional[dict[str, Any]] = None,
        lora_request: Optional[Union[LoRARequest, None]] = None,
        trace_headers: Optional[Mapping[str, str]] = None,
    ) -> Union[list[PoolingRequestOutput], ErrorResponse]:
        request_prompts: list[str] = []
        engine_prompts: list[TokensPrompt] = []

        if len(data_1) == 1:
            data_1 = data_1 * len(data_2)

        if isinstance(tokenizer, MistralTokenizer):
            raise ValueError(
                "MistralTokenizer not supported for cross-encoding")

        tokenization_kwargs = tokenization_kwargs or {}

        input_pairs = [(t1, t2) for t1, t2 in zip(data_1, data_2)]

        if self.model_config.is_multimodal_model:

            preprocess_async = make_async(self._preprocess_score,
                                          executor=self._tokenizer_executor)

            preprocessed_prompts = await asyncio.gather(
                *(preprocess_async(request=request,
                                   tokenizer=tokenizer,
                                   tokenization_kwargs=tokenization_kwargs,
                                   data_1=t1,
                                   data_2=t2) for t1, t2 in input_pairs))

            for full_prompt, engine_prompt in preprocessed_prompts:
                request_prompts.append(full_prompt)
                engine_prompts.append(engine_prompt)

        else:
            tokenize_async = make_async(tokenizer.__call__,
                                        executor=self._tokenizer_executor)
            use_pad_token = self.model_config.use_pad_token

            if use_pad_token:
                # cross_encoder models defaults to using pad_token.
                tokenized_prompts = await asyncio.gather(*(
                    tokenize_async(
                        text=t1,  # type: ignore[arg-type]
                        text_pair=t2,  # type: ignore[arg-type]
                        **tokenization_kwargs) for t1, t2 in input_pairs))
            else:
                # `llm as reranker` models defaults to not using pad_token.
                tokenized_prompts = await asyncio.gather(*(
                    tokenize_async(
                        text=t1 +  # type: ignore[operator]
                        t2,
                        **tokenization_kwargs) for t1, t2 in input_pairs))

            for prompt_inputs, (t1, t2) in zip(tokenized_prompts, input_pairs):
                sep_token = tokenizer.sep_token if (tokenizer.sep_token
                                                    and use_pad_token) else ''
                request_prompt = f"{t1}{sep_token}{t2}"

                input_ids = prompt_inputs["input_ids"]
                text_token_prompt = \
                    self._validate_input(request, input_ids, request_prompt)
                engine_prompt = TokensPrompt(
                    prompt_token_ids=text_token_prompt["prompt_token_ids"],
                    token_type_ids=prompt_inputs.get("token_type_ids"))

                request_prompts.append(request_prompt)
                engine_prompts.append(engine_prompt)

        # Schedule the request and get the result generator.
        generators: list[AsyncGenerator[PoolingRequestOutput, None]] = []

        pooling_params = request.to_pooling_params()

        try:
            pooling_params.verify("score", self.model_config)
        except ValueError as e:
            return self.create_error_response(str(e))

        for i, engine_prompt in enumerate(engine_prompts):
            request_id_item = f"{request_id}-{i}"

            self._log_inputs(request_id_item,
                             request_prompts[i],
                             params=pooling_params,
                             lora_request=lora_request)

            generator = self.engine_client.encode(
                engine_prompt,
                pooling_params,
                request_id_item,
                lora_request=lora_request,
                trace_headers=trace_headers,
                priority=request.priority,
            )

            generators.append(generator)

        result_generator = merge_async_iterators(*generators)

        # Non-streaming response
        final_res_batch: list[
            Optional[PoolingRequestOutput]] = [None] * len(engine_prompts)

        async for i, res in result_generator:
            final_res_batch[i] = res

        return [out for out in final_res_batch if out is not None]

    async def _run_scoring(
        self,
        data_1: Union[list[str], str, ScoreMultiModalParam],
        data_2: Union[list[str], str, ScoreMultiModalParam],
        request: Union[ScoreRequest, RerankRequest],
        request_id: str,
        raw_request: Optional[Request] = None,
        truncate_prompt_tokens: Optional[int] = None,
    ) -> Union[list[PoolingRequestOutput], ErrorResponse]:
        lora_request = self._maybe_get_adapters(request)

        tokenizer = await self.engine_client.get_tokenizer(lora_request)

        tokenization_kwargs: dict[str, Any] = {}
        _validate_truncation_size(self.max_model_len, truncate_prompt_tokens,
                                  tokenization_kwargs)

        trace_headers = (None if raw_request is None else await
                         self._get_trace_headers(raw_request.headers))

        if not self.model_config.is_multimodal_model and (isinstance(
                data_1, dict) or isinstance(data_2, dict)):
            raise ValueError(
                f"MultiModalParam is not supported for {self.model_config.architecture}"  # noqa: E501
            )

        if isinstance(data_1, str):
            data_1 = [data_1]
        elif isinstance(data_1, dict):
            data_1 = data_1.get("content")  # type: ignore[assignment]

        if isinstance(data_2, str):
            data_2 = [data_2]
        elif isinstance(data_2, dict):
            data_2 = data_2.get("content")  # type: ignore[assignment]

        _validate_score_input_lens(data_1, data_2)  # type: ignore[arg-type]

        if self.model_config.is_cross_encoder:
            return await self._cross_encoding_score(
                tokenizer=tokenizer,
                data_1=data_1,  # type: ignore[arg-type]
                data_2=data_2,  # type: ignore[arg-type]
                request=request,
                request_id=request_id,
                tokenization_kwargs=tokenization_kwargs,
                lora_request=lora_request,
                trace_headers=trace_headers)

        else:
            return await self._embedding_score(
                tokenizer=tokenizer,
                texts_1=data_1,  # type: ignore[arg-type]
                texts_2=data_2,  # type: ignore[arg-type]
                request=request,
                request_id=request_id,
                tokenization_kwargs=tokenization_kwargs,
                lora_request=lora_request,
                trace_headers=trace_headers)

    async def create_score(
        self,
        request: ScoreRequest,
        raw_request: Optional[Request] = None,
    ) -> Union[ScoreResponse, ErrorResponse]:
        """
        Score API similar to Sentence Transformers cross encoder

        See https://sbert.net/docs/package_reference/cross_encoder
        """
        error_check_ret = await self._check_model(request)
        if error_check_ret is not None:
            return error_check_ret

        request_id = f"score-{self._base_request_id(raw_request)}"
        created_time = int(time.time())

        try:
            final_res_batch = await self._run_scoring(
                request.text_1,
                request.text_2,
                request,
                request_id,
                raw_request,
                request.truncate_prompt_tokens,
            )
            if isinstance(final_res_batch, ErrorResponse):
                return final_res_batch

            return self.request_output_to_score_response(
                final_res_batch,
                request_id,
                created_time,
                self._get_model_name(request.model),
            )
        except asyncio.CancelledError:
            return self.create_error_response("Client disconnected")
        except ValueError as e:
            # TODO: Use a vllm-specific Validation Error
            return self.create_error_response(str(e))

    async def do_rerank(
        self,
        request: RerankRequest,
        raw_request: Optional[Request] = None
    ) -> Union[RerankResponse, ErrorResponse]:
        """
        Rerank API based on JinaAI's rerank API; implements the same
        API interface. Designed for compatibility with off-the-shelf
        tooling, since this is a common standard for reranking APIs

        See example client implementations at
        https://github.com/infiniflow/ragflow/blob/main/rag/llm/rerank_model.py
        numerous clients use this standard.
        """
        error_check_ret = await self._check_model(request)
        if error_check_ret is not None:
            return error_check_ret

        request_id = f"rerank-{self._base_request_id(raw_request)}"
        documents = request.documents
        top_n = request.top_n if request.top_n > 0 else (
            len(documents)
            if isinstance(documents, list) else len(documents["content"]))

        try:
            final_res_batch = await self._run_scoring(
                request.query,
                documents,
                request,
                request_id,
                raw_request,
                request.truncate_prompt_tokens,
            )
            if isinstance(final_res_batch, ErrorResponse):
                return final_res_batch

            return self.request_output_to_rerank_response(
                final_res_batch,
                request_id,
                self._get_model_name(request.model),
                documents,
                top_n,
            )
        except asyncio.CancelledError:
            return self.create_error_response("Client disconnected")
        except ValueError as e:
            # TODO: Use a vllm-specific Validation Error
            return self.create_error_response(str(e))

    def request_output_to_score_response(
        self,
        final_res_batch: list[PoolingRequestOutput],
        request_id: str,
        created_time: int,
        model_name: str,
    ) -> ScoreResponse:
        items: list[ScoreResponseData] = []
        num_prompt_tokens = 0

        for idx, final_res in enumerate(final_res_batch):
            classify_res = ScoringRequestOutput.from_base(final_res)

            item = ScoreResponseData(
                index=idx,
                score=classify_res.outputs.score,
            )
            prompt_token_ids = final_res.prompt_token_ids

            items.append(item)
            num_prompt_tokens += len(prompt_token_ids)

        usage = UsageInfo(
            prompt_tokens=num_prompt_tokens,
            total_tokens=num_prompt_tokens,
        )

        return ScoreResponse(
            id=request_id,
            created=created_time,
            model=model_name,
            data=items,
            usage=usage,
        )

    def request_output_to_rerank_response(
            self, final_res_batch: list[PoolingRequestOutput], request_id: str,
            model_name: str, documents: Union[list[str], ScoreMultiModalParam],
            top_n: int) -> RerankResponse:
        """
        Convert the output of do_rank to a RerankResponse
        """
        results: list[RerankResult] = []
        num_prompt_tokens = 0
        for idx, final_res in enumerate(final_res_batch):
            classify_res = ScoringRequestOutput.from_base(final_res)

            result = RerankResult(
                index=idx,
                document=RerankDocument(text=documents[idx]) if isinstance(
                    documents, list) else RerankDocument(
                        multi_modal=documents["content"][idx]),
                relevance_score=classify_res.outputs.score,
            )
            results.append(result)
            prompt_token_ids = final_res.prompt_token_ids
            num_prompt_tokens += len(prompt_token_ids)

        # sort by relevance, then return the top n if set
        results.sort(key=lambda x: x.relevance_score, reverse=True)
        if top_n < len(documents):
            results = results[:top_n]

        return RerankResponse(
            id=request_id,
            model=model_name,
            results=results,
            usage=RerankUsage(total_tokens=num_prompt_tokens))
