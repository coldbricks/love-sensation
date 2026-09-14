"""Offline, image-level covered-rear evidence from a pinned public SigLIP2 model.

Asset provisioning is separate from inference. Image tensors never leave the
local process. A signed text-similarity margin is not a detector confidence or
localization; callers must retain that distinction when reviewing the result.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import tempfile
import urllib.request

MODEL_ID = "google/siglip2-so400m-patch16-512"
MODEL_REVISION = "ceea1cba8130d8271436da4828633198c176a775"
MODEL_SUBDIRECTORY = "siglip2-so400m-patch16-512"
ASSETS = (
    ("config.json", 537, "048fd125a0ce9fdf98919bb3651c8596510488282cf76402eb838418a8029c79"),
    ("model.safetensors", 4546331880, "a621bd212e1b3329b428595f9693217e19587afe826adf3e5c241a16392e8973"),
    ("preprocessor_config.json", 394, "63ff380d3e424f93e6fbca5cc8e74eeed882e96fd6be2b7728aed308f3ad1513"),
    ("special_tokens_map.json", 636, "baec30ea10906f16adb8c18af7a34023002c1746542612b8b41c9f09e1351351"),
    ("tokenizer.json", 34363039, "cb9140fae3ac5122c972d37adf83e1248471a38147ad76f8215c8872c6fd8322"),
    ("tokenizer.model", 4241003, "61a7b147390c64585d6c3543dd6fc636906c9af3865a5548f27f31aee1d4c8e2"),
    ("tokenizer_config.json", 47164, "14afe629fe4959b9e0d51e1852b8d9f7ad074f90a1a7125a4fcdd17f06e78fc8"),
)
PROMPTS = (
    ("positive", "A photo of a fully clothed person seen from behind, with their buttocks covered by pants."),
    ("positive", "A back view of a person wearing jeans or trousers."),
    ("positive", "A photo of a clothed person bending forward, with the rear of their pants visible."),
    ("positive", "A close view of a person's buttocks covered by clothing."),
    ("negative", "A front view of a fully clothed person."),
    ("negative", "A portrait showing a person's face and upper body."),
    ("negative", "A close view of shoes or feet."),
    ("negative", "An empty room or a scene with no person."),
    ("negative", "A piece of clothing or fabric with no person wearing it."),
    ("negative", "An outdoor scene of buildings, a vehicle, or pavement."),
    ("negative", "A corrupted video frame with green areas, block artifacts, or missing picture data."),
    ("negative", "A flat blank image or random colored noise."),
)


def _canonical_hash(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


PROMPT_REVISION = "covered-rear-v1:" + _canonical_hash(PROMPTS)
PREPROCESS_REVISION = "official-torchvision-512-bilinear-rescale-normalize-v1"


def _file_hash(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def ensure_semantic_model(model_dir: Path, emit=lambda event: None, *, download: bool = True) -> Path:
    """Provision public assets before any private image is opened; check every byte."""
    directory = Path(model_dir) / MODEL_SUBDIRECTORY
    directory.mkdir(parents=True, exist_ok=True)
    for name, size, digest in ASSETS:
        path = directory / name
        if path.is_file() and path.stat().st_size == size and _file_hash(path) == digest:
            continue
        if not download:
            raise RuntimeError(f"The local covered-body model asset is missing or invalid: {name}")
        emit({"type": "status", "message": f"Downloading the public covered-body model: {name} (4.6 GB total, once)..."})
        url = f"https://huggingface.co/{MODEL_ID}/resolve/{MODEL_REVISION}/{name}"
        temporary = None
        try:
            request = urllib.request.Request(url, headers={"User-Agent": "LoveSensation/2"})
            with urllib.request.urlopen(request, timeout=300) as response:
                with tempfile.NamedTemporaryFile(dir=directory, prefix=name + ".", suffix=".download", delete=False) as destination:
                    temporary = Path(destination.name)
                    shutil.copyfileobj(response, destination, length=1024 * 1024)
            if temporary.stat().st_size != size or _file_hash(temporary) != digest:
                raise RuntimeError(f"Downloaded covered-body model failed its size/SHA-256 check: {name}")
            temporary.replace(path)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
    return directory


def semantic_fingerprint(*, precision: str, processor_config: dict, runtime_versions: dict) -> str:
    return "siglip2:" + _canonical_hash({
        "model": MODEL_ID, "revision": MODEL_REVISION, "assets": ASSETS,
        "prompts": PROMPTS, "prompt_revision": PROMPT_REVISION,
        "preprocessing": PREPROCESS_REVISION, "processor_config": processor_config,
        "precision": precision, "runtime_versions": runtime_versions,
        "text_padding": "max_length64", "attention": "sdpa",
        "logits": "normalized-fp16-embeddings-fp32-math-v1",
    })


def evidence_record(positive_logit: float, negative_logit: float) -> dict:
    positive_logit, negative_logit = float(positive_logit), float(negative_logit)
    margin = positive_logit - negative_logit
    if not all(math.isfinite(value) for value in (positive_logit, negative_logit, margin)):
        raise ValueError("Covered-body inference returned non-finite evidence.")
    return {"class": "BUTTOCKS_COVERED", "box": [], "source": "siglip2", "scope": "image",
            "score_kind": "logit_margin", "raw_margin": margin,
            "positive_logit": positive_logit, "negative_logit": negative_logit,
            "model_revision": MODEL_REVISION, "prompt_revision": PROMPT_REVISION}


class CoveredSemantic:
    """GPU-default semantic inference with a fixed prompt set and no remote calls."""

    def __init__(self, directory: Path, device, emit=lambda event: None):
        # Only verified local files are passed to Transformers. There is no Hub
        # client, URL, image path, or private encoded buffer in the inference API.
        os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
        import torch
        import torch.nn.functional as functional
        import transformers
        import torchvision
        from transformers import AutoModel, AutoProcessor

        self.torch, self.functional, self.device = torch, functional, device
        self.cuda = device.type == "cuda"
        self.dtype = torch.float16 if self.cuda else torch.float32
        emit({"type": "status", "message": "Loading the local covered-body model and verifying its compute device..."})
        self.model = AutoModel.from_pretrained(
            directory, local_files_only=True, trust_remote_code=False,
            dtype=self.dtype, attn_implementation="sdpa",
        ).to(device).eval()
        self.processor = AutoProcessor.from_pretrained(directory, local_files_only=True, trust_remote_code=False)
        if not all(parameter.device == device and parameter.dtype == self.dtype for parameter in self.model.parameters()):
            raise RuntimeError("Covered-body model did not load on the requested device and precision.")
        text_inputs = self.processor.tokenizer(
            [text for _, text in PROMPTS], padding="max_length", max_length=64,
            truncation=True, return_tensors="pt",
        ).to(device)
        with torch.inference_mode():
            features = self.model.get_text_features(**text_inputs).pooler_output.float()
            self.text_features = functional.normalize(features, dim=-1)
        self.positive_indices = [index for index, (group, _) in enumerate(PROMPTS) if group == "positive"]
        self.negative_indices = [index for index, (group, _) in enumerate(PROMPTS) if group == "negative"]
        processor_config = self.processor.image_processor.to_dict()
        self.fingerprint = semantic_fingerprint(
            precision=str(self.dtype), processor_config=processor_config,
            runtime_versions={"transformers": transformers.__version__, "torch": torch.__version__, "torchvision": torchvision.__version__},
        )
        observed = {}
        def record_device(module, arguments, output):
            observed.update(device=str(output.device), dtype=str(output.dtype))
        hook = self.model.vision_model.embeddings.patch_embedding.register_forward_hook(record_device)
        try:
            self.predict([torch.zeros(3, 512, 512, device=device, dtype=torch.uint8)])
            if self.cuda:
                torch.cuda.synchronize(device)
        finally:
            hook.remove()
        if observed != {"device": str(device), "dtype": str(self.dtype)}:
            raise RuntimeError("Covered-body warm-up did not run on the requested compute device.")
        self.info = {
            "model": MODEL_ID, "model_revision": MODEL_REVISION, "prompt_revision": PROMPT_REVISION,
            "parameters": sum(parameter.numel() for parameter in self.model.parameters()),
            "device": str(device), "precision": str(self.dtype), "verified_projection": observed,
            "cuda_verified": self.cuda, "preprocessing": str(device),
            "processor_config": processor_config, "local_files_only": True, "trust_remote_code": False,
            "scope": "image", "score_kind": "logit_margin", "localization_available": False,
        }

    def predict(self, images: list) -> list[dict]:
        """Return signed raw evidence for every tensor, including negative images."""
        if not images:
            return []
        if any(image.device != self.device for image in images):
            raise ValueError("Covered-body preprocessing requires tensors on the inference device.")
        torch = self.torch
        with torch.inference_mode():
            pixels = self.processor.image_processor(images=images, return_tensors="pt", device=self.device)["pixel_values"].to(self.dtype)
            if pixels.device != self.device:
                raise RuntimeError("Covered-body preprocessing left the requested device.")
            features = self.model.get_image_features(pixel_values=pixels).pooler_output.float()
            features = self.functional.normalize(features, dim=-1)
            logits = (features @ self.text_features.T) * self.model.logit_scale.float().exp() + self.model.logit_bias.float()
            positive = logits[:, self.positive_indices].amax(dim=-1)
            negative = logits[:, self.negative_indices].amax(dim=-1)
            values = torch.stack((positive, negative), dim=-1).cpu().tolist()
        return [evidence_record(pos, neg) for pos, neg in values]
