#!/usr/bin/env python3
"""
Lab Propuesto 4.A -- Paso 3: MCP Server con mTLS + verificacion de JWT + role-check.

No toca ningun LLM -- el
role-check (ROLE_TOOL_ALLOWLIST) es el enforcement point real, evaluado en
Python puro antes de ejecutar la tool, sea quien sea el que la haya pedido
(agente Gemini, agente Ollama, o un curl a mano).

pip install fastapi "uvicorn[standard]" "python-jose[cryptography]"
(ya instalado en venv_jwt/, ver README.md)

Correr: ./venv_jwt/bin/python3 server.py
"""
import ssl
from fastapi import FastAPI, Header, HTTPException
from jose import jwt, JWTError

JWT_PUBLIC_KEY = open("jwt_public.pem").read()

app = FastAPI()

TOOL_REGISTRY = {
    "get_inventory_count": {"description": "Returns inventory for a part number."},
    "check_stock_location": {"description": "Returns warehouse location for a part."},
    "get_budget_summary": {"description": "Returns quarterly budget summary."},
}

ROLE_TOOL_ALLOWLIST = {
    "logistics-agent": {"get_inventory_count", "check_stock_location"},
    "finance-agent": {"get_budget_summary"},
}


def execute_tool(tool_name: str, params: dict) -> dict:
    """Stub de ejecucion -- en un MCP Server real llamaria al backend correspondiente."""
    if tool_name == "get_inventory_count":
        return {"part": params.get("part_number", "?"), "count": 42}
    if tool_name == "check_stock_location":
        return {"part": params.get("part_number", "?"), "warehouse": "WH-04"}
    if tool_name == "get_budget_summary":
        return {"quarter": "Q4", "total": 250_000}
    raise HTTPException(status_code=404, detail=f"Tool desconocida: {tool_name}")


@app.get("/tools/list_tools")
async def list_tools():
    return {"tools": [{"name": k, **v} for k, v in TOOL_REGISTRY.items()]}


@app.post("/tools/call")
async def call_tool(request: dict, authorization: str = Header(...)):
    token = authorization.removeprefix("Bearer ").strip()
    try:
        payload = jwt.decode(token, JWT_PUBLIC_KEY, algorithms=["RS256"])
    except JWTError as e:
        raise HTTPException(status_code=401, detail=f"JWT inválido: {e}")

    agent_role = payload.get("role")
    tool_name = request.get("name")
    allowed = ROLE_TOOL_ALLOWLIST.get(agent_role, set())
    print(f"[role-check] role={agent_role!r} tool={tool_name!r} allowed={tool_name in allowed}")
    if tool_name not in allowed:
        raise HTTPException(status_code=403,
            detail=f"Rol '{agent_role}' no autorizado para tool '{tool_name}'")

    result = execute_tool(tool_name, request.get("params", {}))
    print(f"[tool call ejecutado] {tool_name}({request.get('params', {})}) -> {result}")
    return {"result": result}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        app, host="0.0.0.0", port=8443,
        ssl_keyfile="certs/server.key",
        ssl_certfile="certs/server.crt",
        ssl_ca_certs="certs/ca.crt",
        ssl_cert_reqs=ssl.CERT_REQUIRED,  # exige certificado de cliente valido: esto ES mTLS
    )
