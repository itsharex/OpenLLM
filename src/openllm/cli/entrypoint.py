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
"""CLI utilities for OpenLLM.

This module also contains the SDK to call ``start`` and ``build`` from SDK

Start any LLM:

```python
openllm.start("falcon", model_id='tiiuae/falcon-7b-instruct')
```

Build a BentoLLM

```python
bento = openllm.build("falcon")
```

Import any LLM into local store
```python
bentomodel = openllm.import_model("falcon", model_id='tiiuae/falcon-7b-instruct')
```
"""
from __future__ import annotations
import functools
import http.client
import importlib.machinery
import importlib.util
import inspect
import itertools
import logging
import os
import pkgutil
import re
import subprocess
import sys
import tempfile
import time
import traceback
import typing as t

import attr
import click
import click_option_group as cog
import fs
import fs.copy
import fs.errors
import inflection
import orjson
import yaml
from bentoml_cli.utils import BentoMLCommandGroup
from bentoml_cli.utils import opt_callback
from simple_di import Provide
from simple_di import inject

import bentoml
from bentoml._internal.configuration.containers import BentoMLContainer
from bentoml._internal.models.model import ModelStore

from . import termui
from ._factory import FC
from ._factory import LiteralOutput
from ._factory import _AnyCallable
from ._factory import bettertransformer_option
from ._factory import container_registry_option
from ._factory import fast_option
from ._factory import machine_option
from ._factory import model_id_option
from ._factory import model_name_argument
from ._factory import model_version_option
from ._factory import output_option
from ._factory import quantize_option
from ._factory import serialisation_option
from ._factory import start_command_factory
from ._factory import workers_per_resource_option
from .. import bundle
from .. import client as openllm_client
from .. import playground
from .. import serialisation
from ..exceptions import OpenLLMException
from ..models.auto import CONFIG_MAPPING
from ..models.auto import MODEL_FLAX_MAPPING_NAMES
from ..models.auto import MODEL_MAPPING_NAMES
from ..models.auto import MODEL_TF_MAPPING_NAMES
from ..models.auto import MODEL_VLLM_MAPPING_NAMES
from ..models.auto import AutoConfig
from ..models.auto import AutoLLM
from ..utils import DEBUG
from ..utils import DEBUG_ENV_VAR
from ..utils import ENV_VARS_TRUE_VALUES
from ..utils import OPTIONAL_DEPENDENCIES
from ..utils import QUIET_ENV_VAR
from ..utils import EnvVarMixin
from ..utils import LazyLoader
from ..utils import analytics
from ..utils import bentoml_cattr
from ..utils import codegen
from ..utils import compose
from ..utils import configure_logging
from ..utils import first_not_none
from ..utils import get_debug_mode
from ..utils import get_quiet_mode
from ..utils import infer_auto_class
from ..utils import is_jupyter_available
from ..utils import is_jupytext_available
from ..utils import is_notebook_available
from ..utils import is_torch_available
from ..utils import is_transformers_supports_agent
from ..utils import resolve_user_filepath
from ..utils import set_debug_mode
from ..utils import set_quiet_mode

if t.TYPE_CHECKING:
  import jupytext
  import nbformat
  import torch

  from bentoml._internal.bento import BentoStore
  from bentoml._internal.container import DefaultBuilder

  from .._configuration import LLMConfig
  from .._schema import EmbeddingsOutput
  from .._types import DictStrAny
  from .._types import ListStr
  from .._types import LiteralRuntime
  from .._types import P
  from ..bundle.oci import LiteralContainerRegistry
  from ..bundle.oci import LiteralContainerVersionStrategy
else:
  torch, jupytext, nbformat = LazyLoader("torch", globals(), "torch"), LazyLoader("jupytext", globals(), "jupytext"), LazyLoader("nbformat", globals(), "nbformat")

# NOTE: We need to do this so that overload can register
# correct overloads to typing registry
if sys.version_info[:2] >= (3, 11):
  from typing import overload
else:
  from typing_extensions import overload

logger = logging.getLogger(__name__)

OPENLLM_FIGLET = """\
 ██████╗ ██████╗ ███████╗███╗   ██╗██╗     ██╗     ███╗   ███╗
██╔═══██╗██╔══██╗██╔════╝████╗  ██║██║     ██║     ████╗ ████║
██║   ██║██████╔╝█████╗  ██╔██╗ ██║██║     ██║     ██╔████╔██║
██║   ██║██╔═══╝ ██╔══╝  ██║╚██╗██║██║     ██║     ██║╚██╔╝██║
╚██████╔╝██║     ███████╗██║ ╚████║███████╗███████╗██║ ╚═╝ ██║
 ╚═════╝ ╚═╝     ╚══════╝╚═╝  ╚═══╝╚══════╝╚══════╝╚═╝     ╚═╝
"""

ServeCommand = t.Literal["serve", "serve-grpc"]

@attr.define
class GlobalOptions:
  cloud_context: str | None = attr.field(default=None, converter=attr.converters.default_if_none("default"))

  def with_options(self, **attrs: t.Any) -> t.Self:
    return attr.evolve(self, **attrs)

CmdType = t.TypeVar("CmdType", bound=click.Command)
GrpType = t.TypeVar("GrpType", bound=click.Group)

_object_setattr = object.__setattr__

class OpenLLMCommandGroup(BentoMLCommandGroup):
  NUMBER_OF_COMMON_PARAMS = 5  # parameters in common_params + 1 faked group option header

  @staticmethod
  def common_params(f: t.Callable[P, t.Any]) -> t.Callable[[FC], FC]:
    # The following logics is similar to one of BentoMLCommandGroup
    @cog.optgroup.group("Global options")
    @cog.optgroup.option("-q", "--quiet", envvar=QUIET_ENV_VAR, is_flag=True, default=False, help="Suppress all output.", show_envvar=True)
    @cog.optgroup.option("--debug", "--verbose", "debug", envvar=DEBUG_ENV_VAR, is_flag=True, default=False, help="Print out debug logs.", show_envvar=True)
    @cog.optgroup.option("--do-not-track", is_flag=True, default=False, envvar=analytics.OPENLLM_DO_NOT_TRACK, help="Do not send usage info", show_envvar=True)
    @cog.optgroup.option("--context", "cloud_context", envvar="BENTOCLOUD_CONTEXT", type=click.STRING, default=None, help="BentoCloud context name.", show_envvar=True)
    @click.pass_context
    @functools.wraps(f)
    def wrapper(ctx: click.Context, quiet: bool, debug: bool, cloud_context: str | None, *args: P.args, **attrs: P.kwargs) -> t.Any:
      ctx.obj = GlobalOptions(cloud_context=cloud_context)
      if quiet:
        set_quiet_mode(True)
        if debug: logger.warning("'--quiet' passed; ignoring '--verbose/--debug'")
      elif debug: set_debug_mode(True)
      configure_logging()
      return f(*args, **attrs)

    return wrapper

  @staticmethod
  def usage_tracking(func: t.Callable[P, t.Any], group: click.Group, **attrs: t.Any) -> t.Callable[t.Concatenate[bool, P], t.Any]:
    command_name = attrs.get("name", func.__name__)

    @functools.wraps(func)
    def wrapper(do_not_track: bool, *args: P.args, **attrs: P.kwargs) -> t.Any:
      if do_not_track:
        with analytics.set_bentoml_tracking():
          return func(*args, **attrs)
      start_time = time.time_ns()
      with analytics.set_bentoml_tracking():
        if group.name is None: raise ValueError("group.name should not be None")
        event = analytics.OpenllmCliEvent(cmd_group=group.name, cmd_name=command_name)
        try:
          return_value = func(*args, **attrs)
          duration_in_ms = (time.time_ns() - start_time) / 1e6
          event.duration_in_ms = duration_in_ms
          analytics.track(event)
          return return_value
        except Exception as e:
          duration_in_ms = (time.time_ns() - start_time) / 1e6
          event.duration_in_ms = duration_in_ms
          event.error_type = type(e).__name__
          event.return_code = 2 if isinstance(e, KeyboardInterrupt) else 1
          analytics.track(event)
          raise

    return wrapper

  @staticmethod
  def exception_handling(func: t.Callable[P, t.Any], group: click.Group, **attrs: t.Any) -> t.Callable[P, t.Any]:
    command_name = attrs.get("name", func.__name__)

    @functools.wraps(func)
    def wrapper(*args: P.args, **attrs: P.kwargs) -> t.Any:
      try:
        return func(*args, **attrs)
      except OpenLLMException as err:
        raise click.ClickException(click.style(f"[{group.name}] '{command_name}' failed: " + err.message, fg="red")) from err
      except KeyboardInterrupt:
        pass

    return wrapper

  def get_command(self, ctx: click.Context, cmd_name: str) -> click.Command | None:
    cmd_name = self.resolve_alias(cmd_name)
    if ctx.command.name in _start_mapping:
      try:
        return _start_mapping[ctx.command.name][cmd_name]
      except KeyError:
        # TODO: support start from a bento
        try:
          bentoml.get(cmd_name)
          raise click.ClickException(f"'openllm start {cmd_name}' is currently disabled for the time being. Please let us know if you need this feature by opening an issue on GitHub.")
        except bentoml.exceptions.NotFound:
          pass
        raise click.BadArgumentUsage(f"{cmd_name} is not a valid model identifier supported by OpenLLM.") from None
    return super().get_command(ctx, cmd_name)

  def list_commands(self, ctx: click.Context) -> list[str]:
    if ctx.command.name in {"start", "start-grpc"}: return list(CONFIG_MAPPING.keys())
    return super().list_commands(ctx)

  # NOTE: The following overload are ported from click to make sure
  # cli.command is correctly typed. See https://github.com/pallets/click/blob/main/src/click/decorators.py#L136
  #
  # variant: no call, directly as decorator for a function.
  @overload
  def command(self, name: _AnyCallable) -> click.Command:
    ...

  # variant: with positional name and with positional or keyword cls argument:
  # @command(namearg, CommandCls, ...) or @command(namearg, cls=CommandCls, ...)
  @overload
  def command(self, name: str | None, cls: type[CmdType], **attrs: t.Any) -> t.Callable[[_AnyCallable], CmdType]:
    ...

  # variant: name omitted, cls _must_ be a keyword argument, @command(cmd=CommandCls, ...)
  @overload
  def command(self, name: None = None, *, cls: type[CmdType], **attrs: t.Any) -> t.Callable[[_AnyCallable], CmdType]:
    ...

  # variant: name omitted, only provide keyword arguments, @command(context_settings={})
  @overload
  def command(self, *, cls: type[CmdType], **attrs: t.Any) -> t.Callable[[_AnyCallable], CmdType]:
    ...

  # variant: with optional string name, no cls argument provided.
  @overload
  def command(self, name: t.Optional[str] = ..., cls: None = None, **attrs: t.Any) -> t.Callable[[_AnyCallable], click.Command]:
    ...

  def command(self, name: str | None | _AnyCallable = None, cls: type[CmdType] | None = None, *args: t.Any, **attrs: t.Any) -> click.Command | t.Callable[[_AnyCallable], click.Command | CmdType]:
    """Override the default 'cli.command' with supports for aliases for given command, and it wraps the implementation with common parameters."""
    if "context_settings" not in attrs: attrs["context_settings"] = {}
    if "max_content_width" not in attrs["context_settings"]: attrs["context_settings"]["max_content_width"] = 120
    aliases = attrs.pop("aliases", None)

    def decorator(f: _AnyCallable) -> click.Command:
      name = f.__name__.lower()
      if name.endswith("_command"): name = name[:-8]
      name = name.replace("_", "-")
      attrs.setdefault("cls", cls)
      attrs.setdefault("help", inspect.getdoc(f))
      attrs.setdefault("name", name)

      # Wrap implementation withc common parameters
      wrapped = self.common_params(f)
      # Wrap into OpenLLM tracking
      wrapped = self.usage_tracking(wrapped, self, **attrs)
      # Wrap into exception handling
      wrapped = self.exception_handling(wrapped, self, **attrs)

      # move common parameters to end of the parameters list
      _memo = getattr(wrapped, "__click_params__", None)
      if _memo is None: raise RuntimeError("Click command not register correctly.")
      _object_setattr(wrapped, "__click_params__", _memo[-self.NUMBER_OF_COMMON_PARAMS:] + _memo[:-self.NUMBER_OF_COMMON_PARAMS])
      # NOTE: we need to call super of super to avoid conflict with BentoMLCommandGroup command setup
      cmd = super(BentoMLCommandGroup, self).command(*args, **attrs)(wrapped)
      # NOTE: add aliases to a given commands if it is specified.
      if aliases is not None:
        if not cmd.name: raise ValueError("name is required when aliases are available.")
        self._commands[cmd.name] = aliases
        self._aliases.update({alias: cmd.name for alias in aliases})
      return cmd

    return decorator

  if t.TYPE_CHECKING:
    # variant: no call, directly as decorator for a function.
    @overload
    def group(self, name: _AnyCallable) -> click.Group:
      ...

    # variant: with positional name and with positional or keyword cls argument:
    # @group(namearg, GroupCls, ...) or @group(namearg, cls=GroupCls, ...)
    @overload
    def group(self, name: str | None, cls: type[GrpType], **attrs: t.Any) -> t.Callable[[_AnyCallable], GrpType]:
      ...

    # variant: name omitted, cls _must_ be a keyword argument, @group(cmd=GroupCls, ...)
    @overload
    def group(self, name: None = None, *, cls: t.Type[GrpType], **attrs: t.Any) -> t.Callable[[_AnyCallable], GrpType]:
      ...

    # variant: with optional string name, no cls argument provided.
    @overload
    def group(self, name: str | None = ..., cls: None = None, **attrs: t.Any) -> t.Callable[[_AnyCallable], click.Group]:
      ...

    def group(self, *args: t.Any, **kwargs: t.Any) -> t.Callable[[_AnyCallable], click.Group]:
      ...

@click.group(cls=OpenLLMCommandGroup, context_settings=termui.CONTEXT_SETTINGS, name="openllm")
@click.version_option(None, "--version", "-v")
def cli() -> None:
  """\b
   ██████╗ ██████╗ ███████╗███╗   ██╗██╗     ██╗     ███╗   ███╗
  ██╔═══██╗██╔══██╗██╔════╝████╗  ██║██║     ██║     ████╗ ████║
  ██║   ██║██████╔╝█████╗  ██╔██╗ ██║██║     ██║     ██╔████╔██║
  ██║   ██║██╔═══╝ ██╔══╝  ██║╚██╗██║██║     ██║     ██║╚██╔╝██║
  ╚██████╔╝██║     ███████╗██║ ╚████║███████╗███████╗██║ ╚═╝ ██║
   ╚═════╝ ╚═╝     ╚══════╝╚═╝  ╚═══╝╚══════╝╚══════╝╚═╝     ╚═╝.

  \b
  An open platform for operating large language models in production.
  Fine-tune, serve, deploy, and monitor any LLMs with ease.
  """  # noqa: D205

@cli.group(cls=OpenLLMCommandGroup, context_settings=termui.CONTEXT_SETTINGS, name="start", aliases=["start-http"])
def start_command() -> None:
  """Start any LLM as a REST server.

  \b
  ```bash
  $ openllm <start|start-http> <model_name> --<options> ...
  ```
  """

@cli.group(cls=OpenLLMCommandGroup, context_settings=termui.CONTEXT_SETTINGS, name="start-grpc")
def start_grpc_command() -> None:
  """Start any LLM as a gRPC server.

  \b
  ```bash
  $ openllm start-grpc <model_name> --<options> ...
  ```
  """

_start_mapping = {"start": {key: start_command_factory(start_command, key, _context_settings=termui.CONTEXT_SETTINGS) for key in CONFIG_MAPPING}, "start-grpc": {key: start_command_factory(start_grpc_command, key, _context_settings=termui.CONTEXT_SETTINGS, _serve_grpc=True) for key in CONFIG_MAPPING}}

@cli.command(name="import", aliases=["download"])
@model_name_argument
@click.argument("model_id", type=click.STRING, default=None, metavar="Optional[REMOTE_REPO/MODEL_ID | /path/to/local/model]", required=False)
@click.argument("converter", envvar="CONVERTER", type=click.STRING, default=None, required=False, metavar=None)
@model_version_option
@click.option("--runtime", type=click.Choice(["ggml", "transformers"]), default="transformers", help="The runtime to use for the given model. Default is transformers.")
@output_option
@quantize_option
@machine_option
@click.option("--implementation", type=click.Choice(["pt", "tf", "flax", "vllm"]), default=None, help="The implementation for saving this LLM.")
@serialisation_option
def import_command(model_name: str, model_id: str | None, converter: str | None, model_version: str | None, output: LiteralOutput, runtime: t.Literal["ggml", "transformers"], machine: bool, implementation: LiteralRuntime | None, quantize: t.Literal["int8", "int4", "gptq"] | None, serialisation_format: t.Literal["safetensors", "legacy"],) -> bentoml.Model:
  """Setup LLM interactively.

  It accepts two positional arguments: `model_name` and `model_id`. The first name determine
  the model type to download, and the second one is the optional model id to download.

  \b
  This `model_id` can be either pretrained model id that you can get from HuggingFace Hub, or
  a custom model path from your custom pretrained model. Note that the custom model path should
  contain all files required to construct `transformers.PretrainedConfig`, `transformers.PreTrainedModel`
  and `transformers.PreTrainedTokenizer` objects.

  \b
  Note: This is useful for development and setup for fine-tune.
  This will be automatically called when `ensure_available=True` in `openllm.LLM.for_model`

  \b
  ``--model-version`` is an optional option to save the model. Note that
  this is recommended when the model_id is a custom path. Usually, if you are only using pretrained
  model from HuggingFace Hub, you don't need to specify this. If this is not specified, we will calculate
  the hash from the last modified time from this custom path

  \b
  ```bash
  $ openllm download opt facebook/opt-2.7b
  ```

  \b
  > If ``quantize`` is passed, the model weights will be saved as quantized weights. You should
  > only use this option if you want the weight to be quantized by default. Note that OpenLLM also
  > support on-demand quantisation during initial startup.

  \b
  ## Conversion strategies [EXPERIMENTAL]

  \b
  Some models will include built-in conversion strategies for specific weights format.
  It will be determined via the `CONVERTER` environment variable. Note that this envvar should only be use provisionally as it is not RECOMMENDED to export this
  and save to a ``.env`` file.

  The conversion strategies will have the following format and will be determined per architecture implementation:
  <base_format>-<target_format>

  \b
  For example: the below convert LlaMA-2 model format to hf:

  \b
  ```bash
  $ CONVERTER=llama2-hf openllm import llama /path/to/llama-2
  ```

  > **Note**: This behaviour will override ``--runtime``. Therefore make sure that the LLM contains correct conversion strategies to both GGML and HF.
  """
  llm_config = AutoConfig.for_model(model_name)
  env = EnvVarMixin(model_name, llm_config.default_implementation(), model_id=model_id, runtime=runtime, quantize=quantize)
  impl: LiteralRuntime = first_not_none(implementation, default=env.framework_value)
  llm = infer_auto_class(impl).for_model(model_name, llm_config=llm_config, model_version=model_version, ensure_available=False, serialisation=serialisation_format)
  _previously_saved = False
  try:
    _ref = serialisation.get(llm)
    _previously_saved = True
  except bentoml.exceptions.NotFound:
    if not machine and output == "pretty":
      msg = f"'{model_name}' {'with model_id='+ model_id if model_id is not None else ''} does not exists in local store. Saving to BENTOML_HOME{' (path=' + os.getenv('BENTOML_HOME', BentoMLContainer.bentoml_home.get()) + ')' if get_debug_mode() else ''}..."
      termui.echo(msg, fg="yellow", nl=True)
    _ref = serialisation.get(llm, auto_import=True)
    if impl == "pt" and is_torch_available() and torch.cuda.is_available(): torch.cuda.empty_cache()
  if machine: return _ref
  elif output == "pretty":
    if _previously_saved: termui.echo(f"{model_name} with 'model_id={model_id}' is already setup for framework '{impl}': {_ref.tag!s}", nl=True, fg="yellow")
    else: termui.echo(f"Saved model: {_ref.tag}")
  elif output == "json": termui.echo(orjson.dumps({"previously_setup": _previously_saved, "framework": impl, "tag": str(_ref.tag)}, option=orjson.OPT_INDENT_2).decode())
  else: termui.echo(_ref.tag)
  return _ref

def _start(
    model_name: str, /, *, model_id: str | None = None, timeout: int = 30, workers_per_resource: t.Literal["conserved", "round_robin"] | float | None = None, device: tuple[str, ...] | t.Literal["all"] | None = None, quantize: t.Literal["int8", "int4", "gptq"] | None = None, bettertransformer: bool | None = None, runtime: t.Literal["ggml", "transformers"] = "transformers",
    fast: bool = False, adapter_map: dict[t.LiteralString, str | None] | None = None, framework: LiteralRuntime | None = None, additional_args: ListStr | None = None, _serve_grpc: bool = False, __test__: bool = False, **_: t.Any,
) -> LLMConfig | subprocess.Popen[bytes]:
  """Python API to start a LLM server. These provides one-to-one mapping to CLI arguments.

  For all additional arguments, pass it as string to ``additional_args``. For example, if you want to
  pass ``--port 5001``, you can pass ``additional_args=["--port", "5001"]``

  > **Note**: This will create a blocking process, so if you use this API, you can create a running sub thread
  > to start the server instead of blocking the main thread.

  ``openllm.start`` will invoke ``click.Command`` under the hood, so it behaves exactly the same as the CLI interaction.

  > **Note**: ``quantize`` and ``bettertransformer`` are mutually exclusive.

  Args:
  model_name: The model name to start this LLM
  model_id: Optional model id for this given LLM
  timeout: The server timeout
  workers_per_resource: Number of workers per resource assigned.
  See https://docs.bentoml.org/en/latest/guides/scheduling.html#resource-scheduling-strategy
  for more information. By default, this is set to 1.

  > **Note**: ``--workers-per-resource`` will also accept the following strategies:

  > - ``round_robin``: Similar behaviour when setting ``--workers-per-resource 1``. This is useful for smaller models.

  > - ``conserved``: Thjis will determine the number of available GPU resources, and only assign
  one worker for the LLMRunner. For example, if ther are 4 GPUs available, then ``conserved`` is
  equivalent to ``--workers-per-resource 0.25``.
  device: Assign GPU devices (if available) to this LLM. By default, this is set to ``None``. It also accepts 'all'
  argument to assign all available GPUs to this LLM.
  quantize: Quantize the model weights. This is only applicable for PyTorch models.
  Possible quantisation strategies:
  - int8: Quantize the model with 8bit (bitsandbytes required)
  - int4: Quantize the model with 4bit (bitsandbytes required)
  - gptq: Quantize the model with GPTQ (auto-gptq required)
  bettertransformer: Convert given model to FastTransformer with PyTorch.
  runtime: The runtime to use for this LLM. By default, this is set to ``transformers``. In the future, this will include supports for GGML.
  fast: Enable fast mode. This will skip downloading models, and will raise errors if given model_id does not exists under local store.
  adapter_map: The adapter mapping of LoRA to use for this LLM. It accepts a dictionary of ``{adapter_id: adapter_name}``.
  framework: The framework to use for this LLM. By default, this is set to ``pt``.
  additional_args: Additional arguments to pass to ``openllm start``.
  """
  fast = os.getenv("OPENLLM_FAST", str(fast)).upper() in ENV_VARS_TRUE_VALUES
  llm_config = AutoConfig.for_model(model_name)
  _ModelEnv = EnvVarMixin(model_name, first_not_none(framework, default=llm_config.default_implementation()), model_id=model_id, bettertransformer=bettertransformer, quantize=quantize, runtime=runtime)
  os.environ[_ModelEnv.framework] = _ModelEnv.framework_value

  args: ListStr = ["--runtime", runtime]
  if model_id: args.extend(["--model-id", model_id])
  if timeout: args.extend(["--server-timeout", str(timeout)])
  if workers_per_resource: args.extend(["--workers-per-resource", str(workers_per_resource) if not isinstance(workers_per_resource, str) else workers_per_resource])
  if device and not os.getenv("CUDA_VISIBLE_DEVICES"): args.extend(["--device", ",".join(device)])
  if quantize and bettertransformer: raise OpenLLMException("'quantize' and 'bettertransformer' are currently mutually exclusive.")
  if quantize: args.extend(["--quantize", str(quantize)])
  elif bettertransformer: args.append("--bettertransformer")
  if fast: args.append("--fast")
  if adapter_map: args.extend(list(itertools.chain.from_iterable([["--adapter-id", f"{k}{':'+v if v else ''}"] for k, v in adapter_map.items()])))
  if additional_args: args.extend(additional_args)
  if __test__: args.append("--return-process")

  return start_command_factory(start_command if not _serve_grpc else start_grpc_command, model_name, _context_settings=termui.CONTEXT_SETTINGS, _serve_grpc=_serve_grpc).main(args=args if len(args) > 0 else None, standalone_mode=False)

@inject
def _build(
    model_name: str, /, *, model_id: str | None = None, model_version: str | None = None, quantize: t.Literal["int8", "int4", "gptq"] | None = None, bettertransformer: bool | None = None, adapter_map: dict[str, str | None] | None = None, build_ctx: str | None = None, enable_features: tuple[str, ...] | None = None, workers_per_resource: int | float | None = None, runtime: t.Literal[
        "ggml", "transformers"] = "transformers", dockerfile_template: str | None = None, overwrite: bool = False, container_registry: LiteralContainerRegistry | None = None, container_version_strategy: LiteralContainerVersionStrategy | None = None, push: bool = False, containerize: bool = False, serialisation_format: t.Literal["safetensors", "legacy"] = "safetensors",
    additional_args: list[str] | None = None, bento_store: BentoStore = Provide[BentoMLContainer.bento_store],
) -> bentoml.Bento:
  """Package a LLM into a Bento.

  The LLM will be built into a BentoService with the following structure:
  if ``quantize`` is passed, it will instruct the model to be quantized dynamically during serving time.
  if ``bettertransformer`` is passed, it will instruct the model to apply FasterTransformer during serving time.

  ``openllm.build`` will invoke ``click.Command`` under the hood, so it behaves exactly the same as ``openllm build`` CLI.

  > **Note**: ``quantize`` and ``bettertransformer`` are mutually exclusive.

  Args:
  model_name: The model name to start this LLM
  model_id: Optional model id for this given LLM
  model_version: Optional model version for this given LLM
  quantize: Quantize the model weights. This is only applicable for PyTorch models.
  Possible quantisation strategies:
  - int8: Quantize the model with 8bit (bitsandbytes required)
  - int4: Quantize the model with 4bit (bitsandbytes required)
  - gptq: Quantize the model with GPTQ (auto-gptq required)
  bettertransformer: Convert given model to FastTransformer with PyTorch.
  adapter_map: The adapter mapping of LoRA to use for this LLM. It accepts a dictionary of ``{adapter_id: adapter_name}``.
  build_ctx: The build context to use for building BentoLLM. By default, it sets to current directory.
  enable_features: Additional OpenLLM features to be included with this BentoLLM.
  workers_per_resource: Number of workers per resource assigned.
  See https://docs.bentoml.org/en/latest/guides/scheduling.html#resource-scheduling-strategy
  for more information. By default, this is set to 1.

  > **Note**: ``--workers-per-resource`` will also accept the following strategies:

  > - ``round_robin``: Similar behaviour when setting ``--workers-per-resource 1``. This is useful for smaller models.

  > - ``conserved``: This will determine the number of available GPU resources, and only assign
  one worker for the LLMRunner. For example, if ther are 4 GPUs available, then ``conserved`` is
  equivalent to ``--workers-per-resource 0.25``.
  runtime: The runtime to use for this LLM. By default, this is set to ``transformers``. In the future, this will include supports for GGML.
  dockerfile_template: The dockerfile template to use for building BentoLLM. See
  https://docs.bentoml.com/en/latest/guides/containerization.html#dockerfile-template.
  overwrite: Whether to overwrite the existing BentoLLM. By default, this is set to ``False``.
  push: Whether to push the result bento to BentoCloud. Make sure to login with 'bentoml cloud login' first.
  containerize: Whether to containerize the Bento after building. '--containerize' is the shortcut of 'openllm build && bentoml containerize'.
  Note that 'containerize' and 'push' are mutually exclusive
  container_registry: Container registry to choose the base OpenLLM container image to build from. Default to ECR.
  container_version_strategy: The container version strategy. Default to the latest release of OpenLLM.
  serialisation_format: Serialisation for saving models. Default to 'safetensors', which is equivalent to `safe_serialization=True`
  additional_args: Additional arguments to pass to ``openllm build``.
  bento_store: Optional BentoStore for saving this BentoLLM. Default to the default BentoML local store.

  Returns:
  ``bentoml.Bento | str``: BentoLLM instance. This can be used to serve the LLM or can be pushed to BentoCloud.
  If 'format="container"', then it returns the default 'container_name:container_tag'
  """
  args: ListStr = [sys.executable, "-m", "openllm", "build", model_name, "--machine", "--runtime", runtime, "--serialisation", serialisation_format,]
  if quantize and bettertransformer: raise OpenLLMException("'quantize' and 'bettertransformer' are currently mutually exclusive.")
  if quantize: args.extend(["--quantize", quantize])
  if bettertransformer: args.append("--bettertransformer")
  if containerize and push: raise OpenLLMException("'containerize' and 'push' are currently mutually exclusive.")
  if push: args.extend(["--push"])
  if containerize: args.extend(["--containerize"])
  if model_id: args.extend(["--model-id", model_id])
  if build_ctx: args.extend(["--build-ctx", build_ctx])
  if enable_features: args.extend([f"--enable-features={f}" for f in enable_features])
  if workers_per_resource: args.extend(["--workers-per-resource", str(workers_per_resource)])
  if overwrite: args.append("--overwrite")
  if adapter_map: args.extend([f"--adapter-id={k}{':'+v if v is not None else ''}" for k, v in adapter_map.items()])
  if model_version: args.extend(["--model-version", model_version])
  if dockerfile_template: args.extend(["--dockerfile-template", dockerfile_template])
  if container_registry is None: container_registry = "ecr"
  if container_version_strategy is None: container_version_strategy = "release"
  args.extend(["--container-registry", container_registry, "--container-version-strategy", container_version_strategy])
  if additional_args: args.extend(additional_args)

  try:
    output = subprocess.check_output(args, env=os.environ.copy(), cwd=build_ctx or os.getcwd())
  except subprocess.CalledProcessError as e:
    logger.error("Exception caught while building %s", model_name, exc_info=e)
    if e.stderr: raise OpenLLMException(e.stderr.decode("utf-8")) from None
    raise OpenLLMException(str(e)) from None
  pattern = r"^__tag__:[^:\n]+:[^:\n]+"
  matched = re.search(pattern, output.decode("utf-8").strip(), re.MULTILINE)
  if matched is None: raise ValueError(f"Failed to find tag from output: {output!s}")
  return bentoml.get(matched.group(0).partition(":")[-1], _bento_store=bento_store)

def _import_model(
    model_name: str, /, *, model_id: str | None = None, model_version: str | None = None, runtime: t.Literal["ggml", "transformers"] = "transformers", implementation: LiteralRuntime = "pt", quantize: t.Literal["int8", "int4", "gptq"] | None = None, serialisation_format: t.Literal["legacy", "safetensors"] = "safetensors", additional_args: t.Sequence[str] | None = None,
) -> bentoml.Model:
  """Import a LLM into local store.

  > **Note**: If ``quantize`` is passed, the model weights will be saved as quantized weights. You should
  > only use this option if you want the weight to be quantized by default. Note that OpenLLM also
  > support on-demand quantisation during initial startup.

  ``openllm.download`` will invoke ``click.Command`` under the hood, so it behaves exactly the same as the CLI ``openllm import``.

  > **Note**: ``openllm.start`` will automatically invoke ``openllm.download`` under the hood.

  Args:
  model_name: The model name to start this LLM
  model_id: Optional model id for this given LLM
  model_version: Optional model version for this given LLM
  runtime: The runtime to use for this LLM. By default, this is set to ``transformers``. In the future, this will include supports for GGML.
  implementation: The implementation to use for this LLM. By default, this is set to ``pt``.
  quantize: Quantize the model weights. This is only applicable for PyTorch models.
  Possible quantisation strategies:
  - int8: Quantize the model with 8bit (bitsandbytes required)
  - int4: Quantize the model with 4bit (bitsandbytes required)
  - gptq: Quantize the model with GPTQ (auto-gptq required)
  serialisation_format: Type of model format to save to local store. If set to 'safetensors', then OpenLLM will save model using safetensors.
  Default behaviour is similar to ``safe_serialization=False``.
  additional_args: Additional arguments to pass to ``openllm import``.

  Returns:
  ``bentoml.Model``:BentoModel of the given LLM. This can be used to serve the LLM or can be pushed to BentoCloud.
  """
  args = [model_name, "--runtime", runtime, "--implementation", implementation, "--machine", "--serialisation", serialisation_format,]
  if model_id is not None: args.append(model_id)
  if model_version is not None: args.extend(["--model-version", str(model_version)])
  if additional_args is not None: args.extend(additional_args)
  if quantize is not None: args.extend(["--quantize", quantize])
  return import_command.main(args=args, standalone_mode=False)

def _list_models() -> DictStrAny:
  """List all available models within the local store."""
  return models_command.main(args=["-o", "json", "--show-available", "--machine"], standalone_mode=False)

start, start_grpc, build, import_model, list_models = codegen.gen_sdk(_start, _serve_grpc=False), codegen.gen_sdk(_start, _serve_grpc=True), codegen.gen_sdk(_build), codegen.gen_sdk(_import_model), codegen.gen_sdk(_list_models)

@cli.command(context_settings={"token_normalize_func": inflection.underscore})
@model_name_argument
@model_id_option
@output_option
@machine_option
@click.option("--overwrite", is_flag=True, help="Overwrite existing Bento for given LLM if it already exists.")
@workers_per_resource_option(factory=click, build=True)
@cog.optgroup.group(cls=cog.MutuallyExclusiveOptionGroup, name="Optimisation options")
@quantize_option(factory=cog.optgroup, build=True)
@bettertransformer_option(factory=cog.optgroup)
@click.option("--runtime", type=click.Choice(["ggml", "transformers"]), default="transformers", help="The runtime to use for the given model. Default is transformers.")
@click.option("--enable-features", multiple=True, nargs=1, metavar="FEATURE[,FEATURE]", help="Enable additional features for building this LLM Bento. Available: {}".format(", ".join(OPTIONAL_DEPENDENCIES)))
@click.option("--adapter-id", default=None, multiple=True, metavar="[PATH | [remote/][adapter_name:]adapter_id][, ...]", help="Optional adapters id to be included within the Bento. Note that if you are using relative path, '--build-ctx' must be passed.")
@click.option("--build-ctx", help="Build context. This is required if --adapter-id uses relative path", default=None)
@model_version_option
@click.option("--dockerfile-template", default=None, type=click.File(), help="Optional custom dockerfile template to be used with this BentoLLM.")
@serialisation_option
@container_registry_option
@click.option("--container-version-strategy", type=click.Choice(["release", "latest", "nightly"]), default="release", help="Default container version strategy for the image from '--container-registry'")
@fast_option
@cog.optgroup.group(cls=cog.MutuallyExclusiveOptionGroup, name="Utilities options")
@cog.optgroup.option("--containerize", default=False, is_flag=True, type=click.BOOL, help="Whether to containerize the Bento after building. '--containerize' is the shortcut of 'openllm build && bentoml containerize'.")
@cog.optgroup.option("--push", default=False, is_flag=True, type=click.BOOL, help="Whether to push the result bento to BentoCloud. Make sure to login with 'bentoml cloud login' first.")
@click.pass_context
def build_command(
    ctx: click.Context, /, model_name: str, model_id: str | None, overwrite: bool, output: LiteralOutput, runtime: t.Literal["ggml", "transformers"], quantize: t.Literal["int8", "int4", "gptq"] | None, enable_features: tuple[str, ...] | None, bettertransformer: bool | None, workers_per_resource: float | None, adapter_id: tuple[str, ...], build_ctx: str | None, machine: bool,
    model_version: str | None, dockerfile_template: t.TextIO | None, containerize: bool, push: bool, serialisation_format: t.Literal["safetensors", "legacy"], fast: bool, container_registry: LiteralContainerRegistry, container_version_strategy: LiteralContainerVersionStrategy, **attrs: t.Any,
) -> bentoml.Bento:
  """Package a given models into a Bento.

  \b
  ```bash
  $ openllm build flan-t5 --model-id google/flan-t5-large
  ```

  \b
  > NOTE: To run a container built from this Bento with GPU support, make sure
  > to have https://github.com/NVIDIA/nvidia-container-toolkit install locally.
  """
  if machine: output = "porcelain"
  if enable_features: enable_features = tuple(itertools.chain.from_iterable((s.split(",") for s in enable_features)))

  _previously_built = False

  llm_config = AutoConfig.for_model(model_name)
  env = EnvVarMixin(model_name, llm_config.default_implementation(), model_id=model_id, quantize=quantize, bettertransformer=bettertransformer, runtime=runtime)

  # NOTE: We set this environment variable so that our service.py logic won't raise RuntimeError
  # during build. This is a current limitation of bentoml build where we actually import the service.py into sys.path
  try:
    os.environ.update({"OPENLLM_MODEL": inflection.underscore(model_name), env.runtime: str(env.runtime_value), "OPENLLM_SERIALIZATION": serialisation_format})
    if env.model_id_value: os.environ[env.model_id] = str(env.model_id_value)
    if env.quantize_value: os.environ[env.quantize] = str(env.quantize_value)
    if env.bettertransformer_value: os.environ[env.bettertransformer] = str(env.bettertransformer_value)

    llm = infer_auto_class(env.framework_value).for_model(model_name, llm_config=llm_config, ensure_available=not fast, model_version=model_version, serialisation=serialisation_format, **attrs)

    labels = dict(llm.identifying_params)
    labels.update({"_type": llm.llm_type, "_framework": env.framework_value})
    workers_per_resource = first_not_none(workers_per_resource, default=llm_config["workers_per_resource"])

    with fs.open_fs(f"temp://llm_{llm_config['model_name']}") as llm_fs:
      dockerfile_template_path = None
      if dockerfile_template:
        with dockerfile_template:
          llm_fs.writetext("Dockerfile.template", dockerfile_template.read())
        dockerfile_template_path = llm_fs.getsyspath("/Dockerfile.template")

      adapter_map: dict[str, str | None] | None = None
      if adapter_id:
        if not build_ctx: ctx.fail("'build_ctx' is required when '--adapter-id' is passsed.")
        adapter_map = {}
        for v in adapter_id:
          _adapter_id, *adapter_name = v.rsplit(":", maxsplit=1)
          name = adapter_name[0] if len(adapter_name) > 0 else None
          try:
            resolve_user_filepath(_adapter_id, build_ctx)
            src_folder_name = os.path.basename(_adapter_id)
            src_fs = fs.open_fs(build_ctx)
            llm_fs.makedir(src_folder_name, recreate=True)
            fs.copy.copy_dir(src_fs, _adapter_id, llm_fs, src_folder_name)
            adapter_map[src_folder_name] = name
          # this is the remote adapter, then just added back
          # note that there is a drawback here. If the path of the local adapter
          # path have the same name as the remote, then we currently don't support
          # that edge case.
          except FileNotFoundError:
            adapter_map[_adapter_id] = name
        os.environ["OPENLLM_ADAPTER_MAP"] = orjson.dumps(adapter_map).decode()
      bento_tag = bentoml.Tag.from_taglike(f"{llm.llm_type}-service:{llm.tag.version}".lower().strip())
      try:
        bento = bentoml.get(bento_tag)
        if overwrite:
          if output == "pretty": termui.echo(f"Overwriting existing Bento {bento_tag}", fg="yellow")
          bentoml.delete(bento_tag)
          raise bentoml.exceptions.NotFound(f"Rebuilding existing Bento {bento_tag}") from None
        _previously_built = True
      except bentoml.exceptions.NotFound:
        bento = bundle.create_bento(bento_tag, llm_fs, llm, workers_per_resource=workers_per_resource, adapter_map=adapter_map, quantize=quantize, bettertransformer=bettertransformer, extra_dependencies=enable_features, dockerfile_template=dockerfile_template_path, runtime=runtime, container_registry=container_registry, container_version_strategy=container_version_strategy,)
  except Exception as err:
    raise err from None

  if machine: termui.echo(f"__tag__:{bento.tag}", fg="white")
  elif output == "pretty":
    if not get_quiet_mode() and (not push or not containerize):
      termui.echo("\n" + OPENLLM_FIGLET, fg="white")
      if not _previously_built: termui.echo(f"Successfully built {bento}.", fg="green")
      elif not overwrite: termui.echo(f"'{model_name}' already has a Bento built [{bento}]. To overwrite it pass '--overwrite'.", fg="yellow")
      termui.echo(
          "📖 Next steps:\n\n" + "* Push to BentoCloud with 'bentoml push':\n" + f"    $ bentoml push {bento.tag}\n\n" + "* Containerize your Bento with 'bentoml containerize':\n" + f"    $ bentoml containerize {bento.tag} --opt progress=plain" + "\n\n" + "    Tip: To enable additional BentoML features for 'containerize', " + "use '--enable-features=FEATURE[,FEATURE]' " +
          "[see 'bentoml containerize -h' for more advanced usage]\n", fg="blue",
      )
  elif output == "json":
    termui.echo(orjson.dumps(bento.info.to_dict(), option=orjson.OPT_INDENT_2).decode())
  else:
    termui.echo(bento.tag)

  if push: BentoMLContainer.bentocloud_client.get().push_bento(bento, context=t.cast(GlobalOptions, ctx.obj).cloud_context)
  elif containerize:
    backend = t.cast("DefaultBuilder", os.getenv("BENTOML_CONTAINERIZE_BACKEND", "docker"))
    try:
      bentoml.container.health(backend)
    except subprocess.CalledProcessError:
      raise OpenLLMException(f"Failed to use backend {backend}") from None
    try:
      bentoml.container.build(bento.tag, backend=backend, features=("grpc", "io"))
    except Exception as err:
      raise OpenLLMException(f"Exception caught while containerizing '{bento.tag!s}':\n{err}") from err
  return bento

@cli.command()
@output_option
@click.option("--show-available", is_flag=True, default=False, help="Show available models in local store (mutually exclusive with '-o porcelain').")
@machine_option
@click.pass_context
def models_command(ctx: click.Context, output: LiteralOutput, show_available: bool, machine: bool) -> DictStrAny | None:
  """List all supported models.

  \b
  > NOTE: '--show-available' and '-o porcelain' are mutually exclusive.

  \b
  ```bash
  openllm models --show-available
  ```
  """
  from .._llm import normalise_model_name

  models = tuple(inflection.dasherize(key) for key in CONFIG_MAPPING.keys())
  if output == "porcelain":
    if show_available: raise click.BadOptionUsage("--show-available", "Cannot use '--show-available' with '-o porcelain' (mutually exclusive).")
    termui.echo("\n".join(models), fg="white")
  else:
    failed_initialized: list[tuple[str, Exception]] = []

    json_data: dict[str, dict[t.Literal["architecture", "model_id", "url", "installation", "cpu", "gpu", "runtime_impl"], t.Any] | t.Any] = {}
    converted: list[str] = []
    for m in models:
      config = AutoConfig.for_model(m)
      runtime_impl: tuple[str, ...] = ()
      if config["model_name"] in MODEL_MAPPING_NAMES: runtime_impl += ("pt",)
      if config["model_name"] in MODEL_FLAX_MAPPING_NAMES: runtime_impl += ("flax",)
      if config["model_name"] in MODEL_TF_MAPPING_NAMES: runtime_impl += ("tf",)
      if config["model_name"] in MODEL_VLLM_MAPPING_NAMES: runtime_impl += ("vllm",)
      json_data[m] = {"architecture": config["architecture"], "model_id": config["model_ids"], "cpu": not config["requires_gpu"], "gpu": True, "runtime_impl": runtime_impl, "installation": f'"openllm[{m}]"' if m in OPTIONAL_DEPENDENCIES or config["requirements"] else "openllm",}
      converted.extend([normalise_model_name(i) for i in config["model_ids"]])
      if DEBUG:
        try:
          AutoLLM.for_model(m, llm_config=config)
        except Exception as e:
          failed_initialized.append((m, e))

    ids_in_local_store = {k: [i for i in bentoml.models.list() if "framework" in i.info.labels and i.info.labels["framework"] == "openllm" and "model_name" in i.info.labels and i.info.labels["model_name"] == k] for k in json_data.keys()}
    ids_in_local_store = {k: v for k, v in ids_in_local_store.items() if v}
    local_models: DictStrAny | None = None
    if show_available:
      local_models = {k: [str(i.tag) for i in val] for k, val in ids_in_local_store.items()}

    if machine:
      if show_available: json_data["local"] = local_models
      return json_data
    elif output == "pretty":
      import tabulate

      tabulate.PRESERVE_WHITESPACE = True
      # llm, architecture, url, model_id, installation, cpu, gpu, runtime_impl
      data: list[str | tuple[str, str, list[str], str, t.LiteralString, t.LiteralString, tuple[LiteralRuntime, ...]]] = []
      for m, v in json_data.items():
        data.extend([(m, v["architecture"], v["model_id"], v["installation"], "❌" if not v["cpu"] else "✅", "✅", v["runtime_impl"],)])
      column_widths = [int(termui.COLUMNS / 12), int(termui.COLUMNS / 6), int(termui.COLUMNS / 4), int(termui.COLUMNS / 12), int(termui.COLUMNS / 12), int(termui.COLUMNS / 12), int(termui.COLUMNS / 4),]

      if len(data) == 0 and len(failed_initialized) > 0:
        termui.echo("Exception found while parsing models:\n", fg="yellow")
        for m, err in failed_initialized:
          termui.echo(f"- {m}: ", fg="yellow", nl=False)
          termui.echo(traceback.print_exception(err, limit=3), fg="red")
        sys.exit(1)

      table = tabulate.tabulate(data, tablefmt="fancy_grid", headers=["LLM", "Architecture", "Models Id", "pip install", "CPU", "GPU", "Runtime"], maxcolwidths=column_widths,)
      termui.echo(table, fg="white")

      if DEBUG and len(failed_initialized) > 0:
        termui.echo("\nThe following models are supported but failed to initialize:\n")
        for m, err in failed_initialized:
          termui.echo(f"- {m}: ", fg="blue", nl=False)
          termui.echo(err, fg="red")

      if show_available:
        if len(ids_in_local_store) == 0:
          termui.echo("No models available locally.")
          ctx.exit(0)
        termui.echo("The following are available in local store:", fg="magenta")
        termui.echo(orjson.dumps(local_models, option=orjson.OPT_INDENT_2).decode(), fg="white")
    else:
      if show_available: json_data["local"] = local_models
      termui.echo(orjson.dumps(json_data, option=orjson.OPT_INDENT_2,).decode(), fg="white")
  ctx.exit(0)

@cli.command()
@model_name_argument(required=False)
@click.option("-y", "--yes", "--assume-yes", is_flag=True, help="Skip confirmation when deleting a specific model")
@click.option("--include-bentos/--no-include-bentos", is_flag=True, default=False, help="Whether to also include pruning bentos.")
@inject
def prune_command(model_name: str | None, yes: bool, include_bentos: bool, model_store: ModelStore = Provide[BentoMLContainer.model_store], bento_store: BentoStore = Provide[BentoMLContainer.bento_store]) -> None:
  """Remove all saved models, (and optionally bentos) built with OpenLLM locally.

  \b
  If a model type is passed, then only prune models for that given model type.
  """
  available: list[tuple[bentoml.Model | bentoml.Bento, ModelStore | BentoStore]] = [(m, model_store) for m in bentoml.models.list() if "framework" in m.info.labels and m.info.labels["framework"] == "openllm"]
  if model_name is not None: available = [(m, store) for m, store in available if "model_name" in m.info.labels and m.info.labels["model_name"] == inflection.underscore(model_name)]
  if include_bentos:
    if model_name is not None: available += [(b, bento_store) for b in bentoml.bentos.list() if "start_name" in b.info.labels and b.info.labels["start_name"] == inflection.underscore(model_name)]
    else: available += [(b, bento_store) for b in bentoml.bentos.list() if "_type" in b.info.labels and "_framework" in b.info.labels]

  for store_item, store in available:
    if yes: delete_confirmed = True
    else: delete_confirmed = click.confirm(f"delete {'model' if isinstance(store, ModelStore) else 'bento'} {store_item.tag}?")
    if delete_confirmed:
      store.delete(store_item.tag)
      termui.echo(f"{store_item} deleted from {'model' if isinstance(store, ModelStore) else 'bento'} store.", fg="yellow")

def parsing_instruction_callback(ctx: click.Context, param: click.Parameter, value: list[str] | str | None) -> tuple[str, bool | str] | list[str] | str | None:
  if value is None:
    return value

  if isinstance(value, list):
    # we only parse --text foo bar -> --text foo and omit bar
    value = value[-1]

  key, *values = value.split("=")
  if not key.startswith("--"):
    raise click.BadParameter(f"Invalid option format: {value}")
  key = key[2:]
  if len(values) == 0:
    return key, True
  elif len(values) == 1:
    return key, values[0]
  else:
    raise click.BadParameter(f"Invalid option format: {value}")

def shared_client_options(f: _AnyCallable | None = None, output_value: t.Literal["json", "porcelain", "pretty"] = "pretty") -> t.Callable[[FC], FC]:
  options = [click.option("--endpoint", type=click.STRING, help="OpenLLM Server endpoint, i.e: http://localhost:3000", envvar="OPENLLM_ENDPOINT", default="http://localhost:3000",), click.option("--timeout", type=click.INT, default=30, help="Default server timeout", show_default=True), output_option(default_value=output_value),]
  return compose(*options)(f) if f is not None else compose(*options)

@cli.command()
@click.argument("task", type=click.STRING, metavar="TASK")
@shared_client_options
@click.option("--agent", type=click.Choice(["hf"]), default="hf", help="Whether to interact with Agents from given Server endpoint.", show_default=True)
@click.option("--remote", is_flag=True, default=False, help="Whether or not to use remote tools (inference endpoints) instead of local ones.", show_default=True)
@click.option("--opt", help="Define prompt options. "
              "(format: ``--opt text='I love this' --opt audio:./path/to/audio  --opt image:/path/to/file``)", required=False, multiple=True, callback=opt_callback, metavar="ARG=VALUE[,ARG=VALUE]")
def instruct_command(endpoint: str, timeout: int, agent: t.LiteralString, output: LiteralOutput, remote: bool, task: str, _memoized: DictStrAny, **attrs: t.Any) -> str:
  """Instruct agents interactively for given tasks, from a terminal.

  \b
  ```bash
  $ openllm instruct --endpoint http://12.323.2.1:3000 \\
        "Is the following `text` (in Spanish) positive or negative?" \\
        --text "¡Este es un API muy agradable!"
  ```
  """
  client = openllm_client.HTTPClient(endpoint, timeout=timeout)

  try:
    client.call("metadata")
  except http.client.BadStatusLine:
    raise click.ClickException(f"{endpoint} is neither a HTTP server nor reachable.") from None
  if agent == "hf":
    if not is_transformers_supports_agent(): raise click.UsageError("Transformers version should be at least 4.29 to support HfAgent. Upgrade with 'pip install -U transformers'")
    _memoized = {k: v[0] for k, v in _memoized.items() if v}
    client._hf_agent.set_stream(logger.info)
    if output != "porcelain": termui.echo(f"Sending the following prompt ('{task}') with the following vars: {_memoized}", fg="magenta")
    result = client.ask_agent(task, agent_type=agent, return_code=False, remote=remote, **_memoized)
    if output == "json": termui.echo(orjson.dumps(result, option=orjson.OPT_INDENT_2).decode(), fg="white")
    else: termui.echo(result, fg="white")
    return result
  else:
    raise click.BadOptionUsage("agent", f"Unknown agent type {agent}")

@cli.command()
@shared_client_options(output_value="json")
@click.option("--server-type", type=click.Choice(["grpc", "http"]), help="Server type", default="http", show_default=True)
@click.argument("text", type=click.STRING, nargs=-1)
@machine_option
@click.pass_context
def embed_command(ctx: click.Context, text: tuple[str, ...], endpoint: str, timeout: int, server_type: t.Literal["http", "grpc"], output: LiteralOutput, machine: bool) -> EmbeddingsOutput | None:
  """Get embeddings interactively, from a terminal.

  \b
  ```bash
  $ openllm embed --endpoint http://12.323.2.1:3000 "What is the meaning of life?" "How many stars are there in the sky?"
  ```
  """
  client = openllm_client.HTTPClient(endpoint, timeout=timeout) if server_type == "http" else openllm_client.GrpcClient(endpoint, timeout=timeout)
  try:
    gen_embed = client.embed(text)
  except ValueError:
    raise click.ClickException(f"Endpoint {endpoint} does not support embeddings.") from None
  if machine: return gen_embed
  elif output == "pretty":
    termui.echo("Generated embeddings: ", fg="magenta", nl=False)
    termui.echo(gen_embed.embeddings, fg="white")
    termui.echo("\nNumber of tokens: ", fg="magenta", nl=False)
    termui.echo(gen_embed.num_tokens, fg="white")
  elif output == "json":
    termui.echo(orjson.dumps(bentoml_cattr.unstructure(gen_embed), option=orjson.OPT_INDENT_2).decode(), fg="white")
  else:
    termui.echo(gen_embed.embeddings, fg="white")
  ctx.exit(0)

@cli.command()
@shared_client_options
@click.option("--server-type", type=click.Choice(["grpc", "http"]), help="Server type", default="http", show_default=True)
@click.argument("prompt", type=click.STRING)
@click.option("--sampling-params", help="Define query options. (format: ``--opt temperature=0.8 --opt=top_k:12)", required=False, multiple=True, callback=opt_callback, metavar="ARG=VALUE[,ARG=VALUE]")
@click.pass_context
def query_command(ctx: click.Context, /, prompt: str, endpoint: str, timeout: int, server_type: t.Literal["http", "grpc"], output: LiteralOutput, _memoized: DictStrAny, **attrs: t.Any) -> None:
  """Ask a LLM interactively, from a terminal.

  \b
  ```bash
  $ openllm query --endpoint http://12.323.2.1:3000 "What is the meaning of life?"
  ```
  """
  _memoized = {k: orjson.loads(v[0]) for k, v in _memoized.items() if v}
  if server_type == "grpc": endpoint = re.sub(r"http://", "", endpoint)
  client = openllm_client.HTTPClient(endpoint, timeout=timeout) if server_type == "http" else openllm_client.GrpcClient(endpoint, timeout=timeout)
  input_fg, generated_fg = "magenta", "cyan"
  if output != "porcelain":
    termui.echo("==Input==\n", fg="white")
    termui.echo(f"{prompt}", fg=input_fg)
  res = client.query(prompt, return_response="raw", **{**client.configuration, **_memoized})
  if output == "pretty":
    response = client.llm.postprocess_generate(prompt, res["responses"])
    termui.echo("\n\n==Responses==\n", fg="white")
    termui.echo(response, fg=generated_fg)
  elif output == "json":
    termui.echo(orjson.dumps(res, option=orjson.OPT_INDENT_2).decode(), fg="white")
  else:
    termui.echo(res["responses"], fg="white")
  ctx.exit(0)

def load_notebook_metadata() -> DictStrAny:
  with open(os.path.join(os.path.dirname(playground.__file__), "_meta.yml"), "r") as f:
    content = yaml.safe_load(f)
  if not all("description" in k for k in content.values()): raise ValueError("Invalid metadata file. All entries must have a 'description' key.")
  return content

@cli.command()
@click.argument("output-dir", default=None, required=False)
@click.option("--port", envvar="JUPYTER_PORT", show_envvar=True, show_default=True, default=8888, help="Default port for Jupyter server")
@click.pass_context
def playground_command(ctx: click.Context, output_dir: str | None, port: int) -> None:
  """OpenLLM Playground.

  A collections of notebooks to explore the capabilities of OpenLLM.
  This includes notebooks for fine-tuning, inference, and more.

  All of the script available in the playground can also be run directly as a Python script:
  For example:

  \b
  ```bash
  python -m openllm.playground.falcon_tuned --help
  ```

  \b
  > Note: This command requires Jupyter to be installed. Install it with 'pip install "openllm[playground]"'
  """
  if not is_jupyter_available() or not is_jupytext_available() or not is_notebook_available():
    raise RuntimeError("Playground requires 'jupyter', 'jupytext', and 'notebook'. Install it with 'pip install \"openllm[playground]\"'")
  metadata = load_notebook_metadata()
  _temp_dir = False
  if output_dir is None:
    _temp_dir = True
    output_dir = tempfile.mkdtemp(prefix="openllm-playground-")
  else:
    os.makedirs(os.path.abspath(os.path.expandvars(os.path.expanduser(output_dir))), exist_ok=True)

  termui.echo("The playground notebooks will be saved to: " + os.path.abspath(output_dir), fg="blue")
  for module in pkgutil.iter_modules(playground.__path__):
    if module.ispkg or os.path.exists(os.path.join(output_dir, module.name + ".ipynb")):
      logger.debug("Skipping: %s (%s)", module.name, "File already exists" if not module.ispkg else f"{module.name} is a module")
      continue
    if not isinstance(module.module_finder, importlib.machinery.FileFinder): continue
    termui.echo("Generating notebook for: " + module.name, fg="magenta")
    markdown_cell = nbformat.v4.new_markdown_cell(metadata[module.name]["description"])
    f = jupytext.read(os.path.join(module.module_finder.path, module.name + ".py"))
    f.cells.insert(0, markdown_cell)
    jupytext.write(f, os.path.join(output_dir, module.name + ".ipynb"), fmt="notebook")
  try:
    subprocess.check_output([sys.executable, "-m", "jupyter", "notebook", "--notebook-dir", output_dir, "--port", str(port), "--no-browser", "--debug"])
  except subprocess.CalledProcessError as e:
    termui.echo(e.output, fg="red")
    raise click.ClickException(f"Failed to start a jupyter server:\n{e}") from None
  except KeyboardInterrupt:
    termui.echo("\nShutting down Jupyter server...", fg="yellow")
    if _temp_dir: termui.echo("Note: You can access the generated notebooks in: " + output_dir, fg="blue")
  ctx.exit(0)

_EXT_FOLDER = os.path.abspath(os.path.join(os.path.dirname(__file__), "ext"))

class Extensions(click.MultiCommand):
  def list_commands(self, ctx: click.Context) -> list[str]:
    return sorted([filename[:-3] for filename in os.listdir(_EXT_FOLDER) if filename.endswith(".py") and not filename.startswith("__")])

  def get_command(self, ctx: click.Context, cmd_name: str) -> click.Command | None:
    try:
      mod = __import__(f"openllm.cli.ext.{cmd_name}", None, None, ["cli"])
    except ImportError:
      return None
    return mod.cli

@cli.group(cls=Extensions, name="ext", aliases=["utils"])
def ext_command() -> None:
  """Extension for OpenLLM CLI."""

if __name__ == "__main__": cli()
