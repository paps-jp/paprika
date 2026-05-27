# paprika · agent-service

Thin HTTP wrapper that turns a goal + page state into a single browser
action. By default it talks to **Ollama-served gpt-oss-20b**, but any
OpenAI-tool-calling-capable model that Ollama can serve works the same.

paprika workers (running inside their CT) will call this service with
the current accessibility tree and a free-text goal, then execute the
returned action via CDP. See the top-level
[`docker-compose-agent.yml`](../docker-compose-agent.yml) for the
deployment topology.

## Why text-only?

Earlier sketches used CogAgent (vision-VLM). gpt-oss-20b is text-only,
which means:

- **Smaller GPU**: ~13 GB VRAM (16 GB cards work), vs ~24 GB for CogAgent.
- **Faster inference**: ~1-3 s per step on a 4080/4090 class GPU.
- **Selector-based actions**: the model emits CSS selectors the paprika
  worker hands straight to CDP -- no pixel-to-element resolution dance.
- **Better reasoning**: gpt-oss-20b reasons closer to o3-mini territory,
  helpful for the "decide which link to follow" judgements that drive
  a crawler.

Tradeoff: the agent can't see canvas, image-only buttons, or rendered
icons. If a site is heavily visual we'd switch back to a vision model
later; that's why this lives in its own stand-alone service.

## API

### `POST /act`

Request:

```json
{
  "goal": "Find the top 10 popular articles and click into each one.",
  "url": "https://example.com/articles",
  "ax_tree": "<text outline of accessibility tree>",
  "text_content": "Optional rendered text. Trim aggressively.",
  "history": ["click(selector='a.popular')", "wait(seconds=2)"],
  "max_new_tokens": 512,
  "temperature": 0.0
}
```

Response:

```json
{
  "action": {
    "kind": "click",
    "selector": "article:nth-of-type(1) a.headline",
    "reasoning": "First popular article on the page."
  },
  "raw": "<the message dict the LLM returned, serialised>",
  "inference_ms": 1820,
  "model_name": "gpt-oss:20b"
}
```

`action.kind` is one of `click`, `type`, `press_key`, `scroll`,
`navigate`, `wait`, `done`, or `unknown` (= we couldn't parse a tool
call out of the response). Each kind populates a documented subset of
the other fields; see [`schema.py`](schema.py).

### `GET /health`

```json
{
  "ok": true,
  "ollama_url": "http://ollama:11434",
  "ollama_reachable": true,
  "model_name": "gpt-oss:20b",
  "model_present": true,
  "error": null
}
```

`ok=false` with `model_present=false` usually means you forgot to pull
the model. From the GPU host:

```bash
docker compose -f docker-compose-agent.yml exec ollama ollama pull gpt-oss:20b
```

## Run

```bash
# On the GPU host, from the repo root
docker compose -f docker-compose-agent.yml up -d --build

# Pull gpt-oss-20b into the ollama volume (~13 GB, one-time)
docker compose -f docker-compose-agent.yml exec ollama ollama pull gpt-oss:20b

# Sanity check
curl http://localhost:8001/health
```

## Env vars

| Var | Default | Meaning |
|---|---|---|
| `OLLAMA_URL` | `http://ollama:11434` | Inside compose, the service name is the hostname |
| `MODEL_NAME` | `gpt-oss:20b` | Switch to `gpt-oss:120b` on an H100 |
| `REQUEST_TIMEOUT_S` | `120` | HTTP timeout for the Ollama call |
| `AGENT_PORT` | `8001` | Host port mapping for the agent |
| `OLLAMA_PORT` | `11434` | Host port mapping for Ollama (useful for direct debugging) |
