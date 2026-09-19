#!/usr/bin/env python3
"""
Lab Propuesto 4.A -- Paso 4: cliente (agente) que autentica con mTLS + JWT firmado.

No toca ningun LLM -- es
el cliente HTTP puro que Paso 5 (agent_step5_ollama.py) envuelve como tool
de un Agent ADK.

pip install httpx "python-jose[cryptography]" (ya instalado en venv_jwt/)

Correr: ./venv_jwt/bin/python3 client.py
"""
import ssl
from datetime import datetime, timedelta, timezone
import httpx
from jose import jwt

JWT_PRIVATE_KEY = open("jwt_private.pem").read()

# SSLContext explicito para mTLS. En httpx >=0.28 el par cert=/verify=<str> quedo
# deprecado y falla el handshake; un SSLContext cargado con la CA y el par
# cert/key de cliente es la forma correcta (y compatible) de configurar mTLS.
_ssl_ctx = ssl.create_default_context(ssl.Purpose.SERVER_AUTH, cafile="certs/ca.crt")
_ssl_ctx.load_cert_chain(certfile="certs/client.crt", keyfile="certs/client.key")


def get_agent_jwt(role: str, agent_id: str = "logi-agent-01") -> str:
    payload = {
        "sub": agent_id,
        "role": role,
        "iat": datetime.now(timezone.utc),
        "exp": datetime.now(timezone.utc) + timedelta(minutes=5),
    }
    return jwt.encode(payload, JWT_PRIVATE_KEY, algorithm="RS256")


def call_tool(tool_name: str, role: str, params: dict | None = None):
    token = get_agent_jwt(role)
    with httpx.Client(verify=_ssl_ctx) as http_client:
        resp = http_client.post(
            "https://localhost:8443/tools/call",
            json={"name": tool_name, "params": params or {}},
            headers={"Authorization": f"Bearer {token}"},
        )
    print(f"[{role} → {tool_name}] HTTP {resp.status_code}: {resp.json()}")
    return resp


if __name__ == "__main__":
    # 1. mTLS válido + JWT válido + rol autorizado para la tool → 200
    call_tool("get_inventory_count", role="logistics-agent", params={"part_number": "X-1000"})

    # 2. mTLS válido + JWT válido, pero rol NO autorizado para esta tool → 403
    call_tool("get_budget_summary", role="logistics-agent")

    # 3. Rol correcto para la tool solicitada → 200
    call_tool("get_budget_summary", role="finance-agent")

    # 4. Verificar que mTLS es obligatorio (correr manualmente, sin --cert):
    #    curl https://localhost:8443/tools/list_tools --cacert certs/ca.crt
    #    → falla el TLS handshake: el servidor exige certificado de cliente (ssl_cert_reqs=CERT_REQUIRED)
