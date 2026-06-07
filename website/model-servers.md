# Supported model servers

Recognised even while **Idle**, so the server stays on the dashboard when its model is
unloaded. Per-model VRAM comes from the server's API where available, otherwise it's
attributed from `nvidia-smi`.

| Server | Model name | Per-model VRAM |
|---|---|---|
| **Ollama** | ✅ loaded + pulled catalogue | ✅ via `/api/ps` (validated) |
| **vLLM** | ✅ via `/v1/models` | attributed |
| **llama.cpp / llama-server** | ✅ via `/v1/models` | attributed |
| **LocalAI** | ✅ via `/v1/models` | attributed |
| **HF TGI / TEI** | ✅ via `/info` | attributed |
| **faster-whisper / Speaches** | ✅ via `/v1/models` | attributed |
| **koboldcpp** | ✅ via `/api/v1/model` | attributed |
| **tabbyAPI · text-generation-webui · LM Studio · xinference · Aphrodite · Infinity** | ✅ via `/v1/models` | attributed |
| **SGLang · OpenLLM · LiteLLM · GPUStack · Cortex / Jan · Ramalama · Nexa · mistral.rs** | ✅ via `/v1/models` | attributed |
| **LoRAX** | ✅ via `/info` | attributed |
| **Whisper ASR webservice / WhisperX** | ✅ up via `/openapi.json` (single entry) | attributed |
| **Wyoming (HA voice: faster-whisper / Piper / openWakeWord)** | ✅ via `describe` over TCP | attributed |
| **OpenedAI-Speech** | ✅ via `/v1/models` | attributed |
| **NVIDIA Triton** | ✅ up via `/v2` (single entry) | attributed |
| **Stable Diffusion (A1111 / Forge / SD.Next)** | ✅ via `/sdapi/v1/options` | attributed |
| **InvokeAI** | ✅ via `/api/v2/models/` | attributed |
| **ComfyUI** | ✅ checkpoints via `/object_info` | attributed |

Don't see yours? Adding a probe is a one-liner — append to `PROBES` in `app.py`. Most
servers speak the OpenAI `/v1/models` shape and differ only by port. See
[How it works](how-it-works.md) and [Contributing](contributing.md).

## Who's calling? (caller attribution)

Model-server APIs never reveal *who* is calling them. The monitor works it out from the
outside: it samples each container's own established connections and matches the remote
port to a model server, then attributes **connection-time per caller → server** —
surfaced as the **"Driven by"** breakdown on each server card. It's sampled, so long LLM
streams are tracked reliably while sub-second calls (e.g. embeddings) are approximate;
the hub's own probe traffic is excluded.
