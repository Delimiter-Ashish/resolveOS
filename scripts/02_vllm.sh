#!/usr/bin/env bash
# Serve the model with an OpenAI-compatible API + native tool calling (Qwen2.5 uses the hermes parser).
set -euo pipefail
source "$(dirname "$0")/env.sh"
activate "$LLM_ENV"
if curl -sf "http://127.0.0.1:$LLM_PORT/v1/models" >/dev/null; then echo "vLLM already up"; exit 0; fi
if [ -z "${CUDA_VISIBLE_DEVICES:-}" ]; then
  # scheduler did not pin a GPU: pick the one with the least memory in use
  export CUDA_VISIBLE_DEVICES="$(nvidia-smi --query-gpu=index,memory.used --format=csv,noheader,nounits | sort -t, -k2 -n | head -1 | cut -d, -f1)"
  echo "no GPU pinned by scheduler; using least-used GPU $CUDA_VISIBLE_DEVICES"
fi
export OMP_NUM_THREADS="${OMP_NUM_THREADS:-4}"
nohup vllm serve "$MODEL" \
  --served-model-name resolveos-llm --port "$LLM_PORT" \
  --dtype bfloat16 --max-model-len "${MAX_MODEL_LEN:-8192}" \
  --gpu-memory-utilization "${GPU_UTIL:-0.6}" --max-num-seqs 16 --enforce-eager \
  --enable-auto-tool-choice --tool-call-parser "${TOOL_PARSER:-hermes}" \
  > "$RUNTIME/vllm.log" 2>&1 &
echo $! > "$RUNTIME/vllm.pid"
echo -n "waiting for vLLM"
for i in $(seq 1 360); do
  if curl -sf "http://127.0.0.1:$LLM_PORT/v1/models" >/dev/null; then echo " up"; exit 0; fi
  sleep 5; echo -n "."
done
echo; echo "vLLM failed to start, see $RUNTIME/vllm.log"; exit 1
