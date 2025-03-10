# Copyright 2023 BentoML Team. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from __future__ import annotations
import logging
import typing as t
import bentoml
import openllm
from .configuration_mpt import DEFAULT_PROMPT_TEMPLATE, MPTPromptType
from ..._prompt import process_prompt
from ...utils import generate_labels, is_triton_available
if t.TYPE_CHECKING: import transformers, torch
else: transformers, torch = openllm.utils.LazyLoader("transformers", globals(), "transformers"), openllm.utils.LazyLoader("torch", globals(), "torch")
logger = logging.getLogger(__name__)

def get_mpt_config(model_id_or_path: str, max_sequence_length: int, device: torch.device | str | int | None, device_map: str | None = None, trust_remote_code: bool = True) -> transformers.PretrainedConfig:
  config = transformers.AutoConfig.from_pretrained(model_id_or_path, trust_remote_code=trust_remote_code)
  if hasattr(config, "init_device") and device_map is None and isinstance(device, (str, torch.device)): config.init_device = str(device)
  if hasattr(config, "attn_config") and is_triton_available(): config.attn_config["attn_impl"] = "triton"
  else: logger.debug("'triton' is not available, Flash Attention will use the default Torch implementation. For faster inference, make sure to install triton with 'pip install \"git+https://github.com/openai/triton.git#egg=triton&subdirectory=python\"'")
  # setting max_seq_len
  config.max_seq_len = max_sequence_length
  return config

class MPT(openllm.LLM["transformers.PreTrainedModel", "transformers.GPTNeoXTokenizerFast"]):
  __openllm_internal__ = True

  def llm_post_init(self):
    self.dtype = torch.bfloat16 if torch.cuda.is_available() else torch.float32

  @property
  def import_kwargs(self) -> tuple[dict[str, t.Any], dict[str, t.Any]]:
    return {"torch_dtype": torch.bfloat16 if torch.cuda.is_available() else torch.float32}, {"padding_side": "left"}

  def import_model(self, *args: t.Any, trust_remote_code: bool = True, **attrs: t.Any) -> bentoml.Model:
    _, tokenizer_attrs = self.llm_parameters
    torch_dtype = attrs.pop("torch_dtype", self.dtype)
    device_map = attrs.pop("device_map", None)
    attrs.pop("low_cpu_mem_usage", None)
    config = get_mpt_config(self.model_id, self.config.max_sequence_length, self.device, device_map=device_map, trust_remote_code=trust_remote_code)
    tokenizer = transformers.AutoTokenizer.from_pretrained(self.model_id, **tokenizer_attrs)
    if tokenizer.pad_token_id is None: tokenizer.pad_token = tokenizer.eos_token
    model = transformers.AutoModelForCausalLM.from_pretrained(self.model_id, config=config, torch_dtype=torch_dtype, trust_remote_code=trust_remote_code, device_map=device_map, **attrs)
    try:
      return bentoml.transformers.save_model(self.tag, model, custom_objects={"tokenizer": tokenizer}, labels=generate_labels(self))
    finally:
      torch.cuda.empty_cache()

  def load_model(self, *args: t.Any, **attrs: t.Any) -> transformers.PreTrainedModel:
    torch_dtype = attrs.pop("torch_dtype", self.dtype)
    device_map = attrs.pop("device_map", None)
    trust_remote_code = attrs.pop("trust_remote_code", True)
    config = get_mpt_config(self._bentomodel.path, self.config.max_sequence_length, self.device, device_map=device_map, trust_remote_code=trust_remote_code,)
    model = transformers.AutoModelForCausalLM.from_pretrained(self._bentomodel.path, config=config, trust_remote_code=trust_remote_code, torch_dtype=torch_dtype, device_map=device_map, **attrs)
    model.tie_weights()
    return model

  def sanitize_parameters(self, prompt: str, max_new_tokens: int | None = None, temperature: float | None = None, top_p: float | None = None, prompt_type: MPTPromptType | None = None, use_default_prompt_template: bool = True, **attrs: t.Any,) -> tuple[str, dict[str, t.Any], dict[str, t.Any]]:
    _template = None
    if use_default_prompt_template:
      if prompt_type is None:
        if "instruct" in self.model_id: prompt_type = "instruct"
        elif "storywriter" in self.model_id: prompt_type = "storywriter"
        elif "chat" in self.model_id: prompt_type = "chat"
        else: prompt_type = "default"
      _template = DEFAULT_PROMPT_TEMPLATE(prompt_type)
    return process_prompt(prompt, _template, use_default_prompt_template), {"max_new_tokens": max_new_tokens, "temperature": temperature, "top_p": top_p}, {}

  def postprocess_generate(self, prompt: str, generation_result: t.Sequence[str], **attrs: t.Any) -> str:
    return generation_result[0]

  def generate(self, prompt: str, **attrs: t.Any) -> list[str]:
    llm_config = self.config.model_construct_env(**attrs)
    inputs = self.tokenizer(prompt, return_tensors="pt").to(self.device)
    attrs = {"do_sample": False if llm_config["temperature"] == 0 else True, "eos_token_id": self.tokenizer.eos_token_id, "pad_token_id": self.tokenizer.pad_token_id, "generation_config": llm_config.to_generation_config()}
    with torch.inference_mode():
      if torch.cuda.is_available():
        with torch.autocast("cuda", torch.float16):
          generated_tensors = self.model.generate(**inputs, **attrs)
      else:
        generated_tensors = self.model.generate(**inputs, **attrs)
    return self.tokenizer.batch_decode(generated_tensors, skip_special_tokens=True)
