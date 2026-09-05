"""Optional pinned model loaders and a dedicated Qwen relevance scorer."""

import json
from importlib.resources import files
from .common import IntegrationError

PINS = files("src.integrations").joinpath("model-pins.json")


def pin(name):
    pins = json.loads(PINS.read_text())
    if name not in pins:
        raise IntegrationError(
            "unknown_model", "Model is outside the evaluation registry"
        )
    return pins[name]


def model_path(name, *, download=False):
    from huggingface_hub import snapshot_download

    revision = pin(name)["revision"]
    return snapshot_download(name, revision=revision, local_files_only=not download)


class QwenReranker:
    """CrossEncoder-compatible predict interface using Qwen yes/no logits."""

    MODEL = "Qwen/Qwen3-Reranker-0.6B"

    def __init__(self, *, device="cpu", max_tokens=4096, batch_size=4):
        from transformers import AutoTokenizer, AutoModelForCausalLM

        if not 128 <= max_tokens <= 32768 or not 1 <= batch_size <= 32:
            raise ValueError("invalid reranker bounds")
        path = model_path(self.MODEL)
        self.tokenizer = AutoTokenizer.from_pretrained(
            path, padding_side="left", local_files_only=True
        )
        self.model = (
            AutoModelForCausalLM.from_pretrained(path, local_files_only=True)
            .to(device)
            .eval()
        )
        self.device = device
        self.max_tokens = max_tokens
        self.batch_size = batch_size
        self.no = self.tokenizer.encode("no", add_special_tokens=False)
        self.yes = self.tokenizer.encode("yes", add_special_tokens=False)
        if len(self.no) != 1 or len(self.yes) != 1:
            raise IntegrationError(
                "tokenizer_mismatch", "Expected single-token yes/no labels"
            )

    @staticmethod
    def prompt(query, document):
        return (
            "<|im_start|>system\nJudge whether the Document meets the requirements based on the Query and the Instruct provided. "
            'Note that the answer can only be "yes" or "no".<|im_end|>\n<|im_start|>user\n'
            "<Instruct>: Given a web search query, retrieve relevant passages that answer the query\n"
            "<Query>: "
            + query
            + "\n<Document>: "
            + document
            + "<|im_end|>\n<|im_start|>assistant\n<think>\n\n</think>\n\n"
        )

    def predict(self, pairs):
        import torch

        if len(pairs) > 1000:
            raise IntegrationError("candidate_limit", "Too many reranking candidates")
        output = []
        for start in range(0, len(pairs), self.batch_size):
            batch = pairs[start : start + self.batch_size]
            if any(len(q) + len(d) > 262144 for q, d in batch):
                raise IntegrationError("input_limit", "Reranking text too large")
            inputs = self.tokenizer(
                [self.prompt(q, d) for q, d in batch],
                padding=True,
                return_tensors="pt",
                truncation=False,
            )
            if inputs["input_ids"].shape[1] > self.max_tokens:
                raise IntegrationError(
                    "token_limit", "Passages exceed the reranking context budget"
                )
            with torch.inference_mode():
                logits = self.model(
                    **{k: v.to(self.device) for k, v in inputs.items()}
                ).logits[:, -1, :]
                scores = torch.softmax(
                    logits[:, [self.no[0], self.yes[0]]].float(), dim=-1
                )[:, 1]
            output.extend(scores.cpu().tolist())
        return output
