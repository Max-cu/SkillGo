from __future__ import annotations

import asyncio

from app.model_gateway import ModelGatewayError, OpenAICompatibleGateway


async def main() -> None:
    gateway = OpenAICompatibleGateway()
    try:
        result = await gateway.agent_step(
            messages=[
                {
                    "role": "system",
                    "content": "Return exactly one JSON object and no Markdown.",
                },
                {
                    "role": "user",
                    "content": 'Return this JSON object: {"ok": true}',
                },
            ]
        )
    except ModelGatewayError as exc:
        print(f"MODEL_ERROR code={exc.code} message={exc}")
        raise SystemExit(1) from exc

    valid = result.output.get("ok") is True
    print(f"MODEL_OK model={result.model_name} valid_json={valid}")
    if not valid:
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())
