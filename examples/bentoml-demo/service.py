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

import bentoml
import openllm

model = "dolly-v2"

llm_config = openllm.AutoConfig.for_model(model)
llm_runner = openllm.Runner(model, llm_config=llm_config)

svc = bentoml.Service(name="llm-service", runners=[llm_runner])

@svc.on_startup
def download(_: bentoml.Context):
  llm_runner.download_model()

@svc.api(input=bentoml.io.Text(), output=bentoml.io.Text())
async def prompt(input_text: str) -> str:
  answer = await llm_runner.generate.async_run(input_text)
  return answer[0]["generated_text"]
