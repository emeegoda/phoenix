from contextlib import ExitStack
from itertools import product
from unittest.mock import MagicMock, patch

import numpy as np
import pandas as pd
import pytest
import responses
from aioresponses import CallbackResult, aioresponses
from pandas.testing import assert_frame_equal
from phoenix.experimental.evals import (
    NOT_PARSABLE,
    RAG_RELEVANCY_PROMPT_TEMPLATE_STR,
    OpenAIModel,
    llm_classify,
    run_relevance_eval,
)
from phoenix.experimental.evals.functions.classify import _snap_to_rail
from phoenix.experimental.evals.models.openai import OPENAI_API_KEY_ENVVAR_NAME


@pytest.fixture
@responses.activate
def mock_ratelimit_inspection():
    # Mock OpenAI API request used for reading the rate limit
    headers = {"x-ratelimit-limit-requests": "100_000", "x-ratelimit-limit-tokens": "100_000"}
    responses.add(
        responses.POST,
        "https://api.openai.com/v1/chat/completions",
        json={},
        status=200,
        headers=headers,
    )
    return responses


@pytest.fixture
def classification_dataframe():
    return pd.DataFrame(
        [
            {
                "query": "What is Python?",
                "reference": "Python is a programming language.",
            },
            {
                "query": "What is Python?",
                "reference": "Ruby is a programming language.",
            },
            {"query": "What is C++?", "reference": "C++ is a programming language."},
            {"query": "What is C++?", "reference": "irrelevant"},
        ],
    )


def test_llm_classify(
    classification_dataframe, mock_ratelimit_inspection, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv(OPENAI_API_KEY_ENVVAR_NAME, "sk-0123456789")
    dataframe = classification_dataframe
    keys = list(zip(dataframe["query"], dataframe["reference"]))
    responses = ["relevant", "irrelevant", "\nrelevant ", "unparsable"]
    response_mapping = {key: response for key, response in zip(keys, responses)}

    def response_callback(url, **kwargs):
        request_body = kwargs["data"].decode("utf-8")

        for key in response_mapping:
            query, reference = key
            response = ""
            if query in request_body and reference in request_body:
                response = response_mapping.pop(
                    key
                )  # Remove the key-value pair to avoid reusing the same response
                break
        return CallbackResult(
            status=200,
            payload={
                "choices": [
                    {
                        "message": {
                            "content": response,
                        },
                    }
                ],
                "usage": {
                    "total_tokens": 1,
                },
            },
        )

    with aioresponses() as mocked_aiohttp:
        for _ in range(len(dataframe)):
            mocked_aiohttp.post(
                "https://api.openai.com/v1/chat/completions",
                callback=response_callback,
            )

        with patch.object(OpenAIModel, "_init_tiktoken", return_value=None):
            model = OpenAIModel()

        result = llm_classify(
            dataframe=dataframe,
            template=RAG_RELEVANCY_PROMPT_TEMPLATE_STR,
            model=model,
            rails=["relevant", "irrelevant"],
            verbose=True,
        )

    labels = ["relevant", "irrelevant", "relevant", NOT_PARSABLE]
    assert result.iloc[:, 0].tolist() == labels
    assert_frame_equal(
        result,
        pd.DataFrame(
            data={"label": ["relevant", "irrelevant", "relevant", NOT_PARSABLE]},
        ),
    )


def test_llm_classify_with_function_calls_in_response(
    classification_dataframe, mock_ratelimit_inspection, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv(OPENAI_API_KEY_ENVVAR_NAME, "sk-0123456789")
    dataframe = classification_dataframe
    keys = list(zip(dataframe["query"], dataframe["reference"]))
    responses = ["relevant", "irrelevant", "\nrelevant ", "unparsable"]
    response_mapping = {key: response for key, response in zip(keys, responses)}

    def response_callback(url, **kwargs):
        request_body = kwargs["data"].decode("utf-8")

        for key in response_mapping:
            query, reference = key
            response = ""
            if query in request_body and reference in request_body:
                response = response_mapping.pop(
                    key
                )  # Remove the key-value pair to avoid reusing the same response
                break
        message = {"function_call": {"arguments": f"{{\n  'response': {response}\n}}"}}
        return CallbackResult(
            status=200,
            payload={"choices": [{"message": message}]},
        )

    with aioresponses() as mocked_aiohttp:
        for _ in range(len(dataframe)):
            mocked_aiohttp.post(
                "https://api.openai.com/v1/chat/completions",
                callback=response_callback,
            )

        with patch.object(OpenAIModel, "_init_tiktoken", return_value=None):
            model = OpenAIModel()

        result = llm_classify(
            dataframe=dataframe,
            template=RAG_RELEVANCY_PROMPT_TEMPLATE_STR,
            model=model,
            rails=["relevant", "irrelevant"],
            verbose=True,
        )

    labels = ["relevant", "irrelevant", "relevant", NOT_PARSABLE]
    assert result.iloc[:, 0].tolist() == labels
    assert_frame_equal(
        result,
        pd.DataFrame(
            data={"label": ["relevant", "irrelevant", "relevant", NOT_PARSABLE]},
        ),
    )


def test_llm_classify_function_call_without_explanation(
    classification_dataframe, mock_ratelimit_inspection, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv(OPENAI_API_KEY_ENVVAR_NAME, "sk-0123456789")
    dataframe = classification_dataframe
    keys = list(zip(dataframe["query"], dataframe["reference"]))
    responses = ["relevant", "irrelevant", "\nrelevant ", "unparsable"]
    response_mapping = {key: response for key, response in zip(keys, responses)}

    def response_callback(url, **kwargs):
        request_body = kwargs["data"].decode("utf-8")

        for key in response_mapping:
            query, reference = key
            response = ""
            if query in request_body and reference in request_body:
                response = response_mapping.pop(
                    key
                )  # Remove the key-value pair to avoid reusing the same response
                break
        message = {
            "function_call": {
                "arguments": f"{{\n  \042response\042: \042{response}\042\n}}",
            }
        }
        return CallbackResult(
            status=200,
            payload={"choices": [{"message": message}]},
        )

    with aioresponses() as mocked_aiohttp:
        for _ in range(len(dataframe)):
            mocked_aiohttp.post(
                "https://api.openai.com/v1/chat/completions",
                callback=response_callback,
            )

        with patch.object(OpenAIModel, "_init_tiktoken", return_value=None):
            model = OpenAIModel()

        result = llm_classify(
            dataframe=dataframe,
            template=RAG_RELEVANCY_PROMPT_TEMPLATE_STR,
            model=model,
            rails=["relevant", "irrelevant"],
            provide_explanation=True,
        )
    labels = ["relevant", "irrelevant", "relevant", NOT_PARSABLE]
    assert result.iloc[:, 0].tolist() == labels
    assert_frame_equal(
        result,
        pd.DataFrame(data={"label": labels, "explanation": [None, None, None, None]}),
    )


def test_llm_classify_function_call_with_explanation(
    classification_dataframe, mock_ratelimit_inspection, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv(OPENAI_API_KEY_ENVVAR_NAME, "sk-0123456789")
    dataframe = classification_dataframe
    keys = list(zip(dataframe["query"], dataframe["reference"]))
    responses = ["relevant", "irrelevant", "\nrelevant ", "unparsable"]
    response_mapping = {key: response for key, response in zip(keys, responses)}

    def response_callback(url, **kwargs):
        request_body = kwargs["data"].decode("utf-8")

        for key in response_mapping:
            query, reference = key
            response = ""
            if query in request_body and reference in request_body:
                response = response_mapping.pop(
                    key
                )  # Remove the key-value pair to avoid reusing the same response
                break
        message = {
            "function_call": {
                "arguments": f"{{\n  \042response\042: \042{response}\042, \042explanation\042: \042{1}\042\n}}"  # noqa E501
            }
        }
        return CallbackResult(
            status=200,
            payload={"choices": [{"message": message}]},
        )

    with aioresponses() as mocked_aiohttp:
        for _ in range(len(dataframe)):
            mocked_aiohttp.post(
                "https://api.openai.com/v1/chat/completions",
                callback=response_callback,
            )

        with patch.object(OpenAIModel, "_init_tiktoken", return_value=None):
            model = OpenAIModel()

        result = llm_classify(
            dataframe=dataframe,
            template=RAG_RELEVANCY_PROMPT_TEMPLATE_STR,
            model=model,
            rails=["relevant", "irrelevant"],
            provide_explanation=True,
        )
    labels = ["relevant", "irrelevant", "relevant", NOT_PARSABLE]
    assert result.iloc[:, 0].tolist() == labels
    assert_frame_equal(
        result,
        pd.DataFrame(data={"label": labels, "explanation": ["1", "1", "1", "1"]}),
    )


def test_llm_classify_prints_to_stdout_with_verbose_flag(
    mock_ratelimit_inspection, monkeypatch: pytest.MonkeyPatch, capfd
):
    monkeypatch.setenv(OPENAI_API_KEY_ENVVAR_NAME, "sk-0123456789")
    dataframe = pd.DataFrame(
        [
            {
                "query": "What is Python?",
                "reference": "Python is a programming language.",
            },
            {
                "query": "What is Python?",
                "reference": "Ruby is a programming language.",
            },
            {
                "query": "What is C++?",
                "reference": "C++ is a programming language.",
            },
            {
                "query": "What is C++?",
                "reference": "irrelevant",
            },
        ]
    )

    keys = list(zip(dataframe["query"], dataframe["reference"]))
    responses = ["relevant", "irrelevant", "\nrelevant ", "unparsable"]
    response_mapping = {key: response for key, response in zip(keys, responses)}

    def response_callback(url, **kwargs):
        request_body = kwargs["data"].decode("utf-8")

        for key in response_mapping:
            query, reference = key
            response = ""
            if query in request_body and reference in request_body:
                response = response_mapping.pop(
                    key
                )  # Remove the key-value pair to avoid reusing the same response
                break
        return CallbackResult(
            status=200,
            payload={
                "choices": [
                    {
                        "message": {
                            "content": response,
                        },
                    }
                ],
                "usage": {
                    "total_tokens": 1,
                },
            },
        )

    with aioresponses() as mocked_aiohttp:
        mocked_aiohttp.post(
            "https://api.openai.com/v1/chat/completions",
            status=200,
            callback=response_callback,
            repeat=True,
        )

        with patch.object(OpenAIModel, "_init_tiktoken", return_value=None):
            model = OpenAIModel()

        llm_classify(
            dataframe=dataframe,
            template=RAG_RELEVANCY_PROMPT_TEMPLATE_STR,
            model=model,
            rails=["relevant", "irrelevant"],
            verbose=True,
        )

    out, _ = capfd.readouterr()
    assert "Snapped 'relevant' to rail: relevant" in out, "Snapping events should be printed"
    assert "Snapped 'irrelevant' to rail: irrelevant" in out, "Snapping events should be printed"
    assert "Snapped '\\nrelevant ' to rail: relevant" in out, "Snapping events should be printed"
    assert "Cannot snap 'unparsable' to rails" in out, "Snapping events should be printed"
    assert "OpenAI invocation parameters" in out, "Model-specific information should be printed"
    assert "'model': 'gpt-4', 'temperature': 0.0" in out, "Model information should be printed"
    assert "sk-0123456789" not in out, "Credentials should not be printed out in cleartext"


def test_llm_classify_shows_retry_info_with_verbose_flag(
    mock_ratelimit_inspection, monkeypatch: pytest.MonkeyPatch, capfd
):
    monkeypatch.setenv(OPENAI_API_KEY_ENVVAR_NAME, "sk-0123456789")
    dataframe = pd.DataFrame(
        [
            {
                "query": "What is Python?",
                "reference": "Python is a programming language.",
            },
        ]
    )

    model = OpenAIModel(max_retries=5)

    openai_retry_errors = [
        model._openai_error.Timeout("test timeout"),
        model._openai_error.APIError("test api error"),
        model._openai_error.APIConnectionError("test api connection error"),
        model._openai_error.RateLimitError("test rate limit error"),
        model._openai_error.ServiceUnavailableError("test service unavailable error"),
    ]
    mock_openai = MagicMock()
    mock_openai.side_effect = openai_retry_errors

    with ExitStack() as stack:
        waiting_fn = "phoenix.experimental.evals.models.base.wait_random_exponential"
        stack.enter_context(patch(waiting_fn, return_value=False))
        stack.enter_context(patch.object(OpenAIModel, "_init_tiktoken", return_value=None))
        stack.enter_context(patch.object(model._openai.ChatCompletion, "acreate", mock_openai))
        stack.enter_context(pytest.raises(model._openai_error.ServiceUnavailableError))
        llm_classify(
            dataframe=dataframe,
            template=RAG_RELEVANCY_PROMPT_TEMPLATE_STR,
            model=model,
            rails=["relevant", "irrelevant"],
            verbose=True,
        )

    out, _ = capfd.readouterr()
    assert "Failed attempt 1" in out, "Retry information should be printed"
    assert "test timeout" in out, "Retry information should be printed"
    assert "Failed attempt 2" in out, "Retry information should be printed"
    assert "test api error" in out, "Retry information should be printed"
    assert "Failed attempt 3" in out, "Retry information should be printed"
    assert "test api connection error" in out, "Retry information should be printed"
    assert "Failed attempt 4" in out, "Retry information should be printed"
    assert "test rate limit error" in out, "Retry information should be printed"
    assert "Failed attempt 5" not in out, "Maximum retries should not be exceeded"


def test_llm_classify_does_not_persist_verbose_flag(
    mock_ratelimit_inspection, monkeypatch: pytest.MonkeyPatch, capfd
):
    monkeypatch.setenv(OPENAI_API_KEY_ENVVAR_NAME, "sk-0123456789")
    dataframe = pd.DataFrame(
        [
            {
                "query": "What is Python?",
                "reference": "Python is a programming language.",
            },
        ]
    )

    model = OpenAIModel(max_retries=2)

    openai_retry_errors = [
        model._openai_error.Timeout("test timeout"),
        model._openai_error.APIError("test api error"),
    ]
    mock_openai = MagicMock()
    mock_openai.side_effect = openai_retry_errors

    with ExitStack() as stack:
        waiting_fn = "phoenix.experimental.evals.models.base.wait_random_exponential"
        stack.enter_context(patch(waiting_fn, return_value=False))
        stack.enter_context(patch.object(OpenAIModel, "_init_tiktoken", return_value=None))
        stack.enter_context(patch.object(model._openai.ChatCompletion, "acreate", mock_openai))
        stack.enter_context(pytest.raises(model._openai_error.APIError))
        llm_classify(
            dataframe=dataframe,
            template=RAG_RELEVANCY_PROMPT_TEMPLATE_STR,
            model=model,
            rails=["relevant", "irrelevant"],
            verbose=True,
        )

    out, _ = capfd.readouterr()
    assert "Failed attempt 1" in out, "Retry information should be printed"
    assert "test timeout" in out, "Retry information should be printed"
    assert "Failed attempt 2" not in out, "Retry information should be printed"

    mock_openai.reset_mock()
    mock_openai.side_effect = openai_retry_errors

    with ExitStack() as stack:
        waiting_fn = "phoenix.experimental.evals.models.base.wait_random_exponential"
        stack.enter_context(patch(waiting_fn, return_value=False))
        stack.enter_context(patch.object(OpenAIModel, "_init_tiktoken", return_value=None))
        stack.enter_context(patch.object(model._openai.ChatCompletion, "acreate", mock_openai))
        stack.enter_context(pytest.raises(model._openai_error.APIError))
        llm_classify(
            dataframe=dataframe,
            template=RAG_RELEVANCY_PROMPT_TEMPLATE_STR,
            model=model,
            rails=["relevant", "irrelevant"],
        )

    out, _ = capfd.readouterr()
    assert "Failed attempt 1" not in out, "The `verbose` flag should not be persisted"
    assert "test timeout" not in out, "The `verbose` flag should not be persisted"


def test_run_relevance_eval_standard_dataframe(
    mock_ratelimit_inspection,
    monkeypatch: pytest.MonkeyPatch,
):
    dataframe = pd.DataFrame(
        [
            {
                "query": "What is Python?",
                "reference": [
                    "Python is a programming language.",
                    "Ruby is a programming language.",
                ],
            },
            {
                "query": "Can you explain Python to me?",
                "reference": np.array(
                    [
                        "Python is a programming language.",
                        "Ruby is a programming language.",
                    ]
                ),
            },
            {
                "query": "What is Ruby?",
                "reference": [
                    "Ruby is a programming language.",
                ],
            },
            {
                "query": "What is C++?",
                "reference": [
                    "Ruby is a programming language.",
                    "C++ is a programming language.",
                ],
            },
            {
                "query": "What is C#?",
                "reference": [],
            },
            {
                "query": "What is Golang?",
                "reference": None,
            },
            {
                "query": None,
                "reference": [
                    "Python is a programming language.",
                    "Ruby is a programming language.",
                ],
            },
            {
                "query": None,
                "reference": None,
            },
        ]
    )

    queries = list(dataframe["query"])
    references = list(dataframe["reference"])
    keys = []
    for query, refs in zip(queries, references):
        refs = refs if refs is None else list(refs)
        if query and refs:
            keys.extend(product([query], refs))

    responses = [
        "relevant",
        "irrelevant",
        "relevant",
        "irrelevant",
        "\nrelevant ",
        "unparsable",
        "relevant",
    ]

    response_mapping = {key: response for key, response in zip(keys, responses)}

    def response_callback(url, **kwargs):
        request_body = kwargs["data"].decode("utf-8")
        response = ""
        for key in response_mapping:
            query, reference = key
            if query in request_body and reference in request_body:
                response = response_mapping[key]
                break
        return CallbackResult(
            status=200,
            payload={
                "choices": [
                    {
                        "message": {
                            "content": response,
                        },
                    }
                ],
                "usage": {
                    "total_tokens": 1,
                },
            },
        )

    monkeypatch.setenv(OPENAI_API_KEY_ENVVAR_NAME, "sk-0123456789")
    with aioresponses() as mocked_aiohttp:
        mocked_aiohttp.post(
            "https://api.openai.com/v1/chat/completions",
            status=200,
            callback=response_callback,
            repeat=True,
        )

        with patch.object(OpenAIModel, "_init_tiktoken", return_value=None):
            model = OpenAIModel()

        relevance_classifications = run_relevance_eval(dataframe, model=model)
        assert relevance_classifications == [
            ["relevant", "irrelevant"],
            ["relevant", "irrelevant"],
            ["relevant"],
            [NOT_PARSABLE, "relevant"],
            [],
            [],
            [],
            [],
        ]


def test_run_relevance_eval_openinference_dataframe(
    mock_ratelimit_inspection,
    monkeypatch: pytest.MonkeyPatch,
):
    dataframe = pd.DataFrame(
        [
            {
                "attributes.input.value": "What is Python?",
                "attributes.retrieval.documents": [
                    {"document.content": "Python is a programming language."},
                    {"document.content": "Ruby is a programming language."},
                ],
            },
            {
                "attributes.input.value": "Can you explain Python to me?",
                "attributes.retrieval.documents": np.array(
                    [
                        {"document.content": "Python is a programming language."},
                        {"document.content": "Ruby is a programming language."},
                    ]
                ),
            },
            {
                "attributes.input.value": "What is Ruby?",
                "attributes.retrieval.documents": [
                    {"document.content": "Ruby is a programming language."},
                ],
            },
            {
                "attributes.input.value": "What is C++?",
                "attributes.retrieval.documents": [
                    {"document.content": "Ruby is a programming language."},
                    {"document.content": "C++ is a programming language."},
                ],
            },
            {
                "attributes.input.value": "What is C#?",
                "attributes.retrieval.documents": [],
            },
            {
                "attributes.input.value": "What is Golang?",
                "attributes.retrieval.documents": None,
            },
            {
                "attributes.input.value": None,
                "attributes.retrieval.documents": [
                    {"document.content": "Python is a programming language."},
                    {"document.content": "Ruby is a programming language."},
                ],
            },
            {
                "attributes.input.value": None,
                "attributes.retrieval.documents": None,
            },
        ]
    )

    queries = list(dataframe["attributes.input.value"])
    references = list(dataframe["attributes.retrieval.documents"])
    keys = []
    for query, refs in zip(queries, references):
        refs = refs if refs is None else list(refs)
        if query and refs:
            keys.extend(product([query], refs))
    keys = [(query, ref["document.content"]) for query, ref in keys]

    responses = [
        "relevant",
        "irrelevant",
        "relevant",
        "irrelevant",
        "\nrelevant ",
        "unparsable",
        "relevant",
    ]

    response_mapping = {key: response for key, response in zip(keys, responses)}

    def response_callback(url, **kwargs):
        request_body = kwargs["data"].decode("utf-8")
        response = ""
        for key in response_mapping:
            query, reference = key
            if query in request_body and reference in request_body:
                response = response_mapping[key]
                break
        return CallbackResult(
            status=200,
            payload={
                "choices": [
                    {
                        "message": {
                            "content": response,
                        },
                    }
                ],
                "usage": {
                    "total_tokens": 1,
                },
            },
        )

    monkeypatch.setenv(OPENAI_API_KEY_ENVVAR_NAME, "sk-0123456789")
    with aioresponses() as mocked_aiohttp:
        mocked_aiohttp.post(
            "https://api.openai.com/v1/chat/completions",
            status=200,
            callback=response_callback,
            repeat=True,
        )

        with patch.object(OpenAIModel, "_init_tiktoken", return_value=None):
            model = OpenAIModel()
        relevance_classifications = run_relevance_eval(dataframe, model=model)
        assert relevance_classifications == [
            ["relevant", "irrelevant"],
            ["relevant", "irrelevant"],
            ["relevant"],
            [NOT_PARSABLE, "relevant"],
            [],
            [],
            [],
            [],
        ]


def test_overlapping_rails():
    assert _snap_to_rail("irrelevant", ["relevant", "irrelevant"]) == "irrelevant"
    assert _snap_to_rail("relevant", ["relevant", "irrelevant"]) == "relevant"
    assert _snap_to_rail("irrelevant...", ["irrelevant", "relevant"]) == "irrelevant"
    assert _snap_to_rail("...irrelevant", ["irrelevant", "relevant"]) == "irrelevant"
    # Both rails are present, cannot parse
    assert _snap_to_rail("relevant...irrelevant", ["irrelevant", "relevant"]) is NOT_PARSABLE
    assert _snap_to_rail("Irrelevant", ["relevant", "irrelevant"]) == "irrelevant"
    # One rail appears twice
    assert _snap_to_rail("relevant...relevant", ["irrelevant", "relevant"]) == "relevant"
    assert _snap_to_rail("b b", ["a", "b", "c"]) == "b"
    # More than two rails
    assert _snap_to_rail("a", ["a", "b", "c"]) == "a"
    assert _snap_to_rail(" abc", ["a", "ab", "abc"]) == "abc"
    assert _snap_to_rail("abc", ["abc", "a", "ab"]) == "abc"
