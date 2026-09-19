#!/usr/bin/env python3
"""
Lab Propuesto 4.A -- Paso 5 (opcional): agente ADK real sobre el MCP Server
con mTLS + JWT + role-check, version con modelo local.

Nota de diseño: `server.py` (Paso 3) es un REST API liso
(`POST /tools/call`), no un servidor MCP/SSE compliant -- por eso este
Paso 5 no usa `MCPToolset(connection_params=SseConnectionParams(...))`
(que negociaría un handshake MCP que este server no habla). En su lugar,
una tool function envuelve el cliente mTLS+JWT ya verificado en
`client.py`, resolviendo el punto pedagógico real del paso: un Agent ADK
real consumiendo el endpoint protegido con mTLS+JWT+role-check.

El agente corre con qwen3.5:9b local via LiteLlm (`num_ctx=8192`,
`temperature=0.2`, `reasoning_effort="none"`).

Regla de diseño de tool: el ROL del agente ("logistics-agent") es un parametro de seguridad,
no algo que el usuario controla -- por eso NO se expone como argumento libre
de la tool al LLM. Se fija por codigo en el wrapper `get_inventory_count`,
igual que `agent_clearance` en secure_retrieve_tool (Lab 5.A). El LLM solo
puede pasar `part_number`, que es lo unico que el usuario efectivamente
pide.

Requiere: certs/ generados (gen_certs.sh), jwt_private.pem/jwt_public.pem
(generate_jwt_keys.py), server.py corriendo en :8443, y Ollama corriendo
con qwen3.5:9b ya descargado. Correr con el venv del lab (tiene
python-jose + google-adk + litellm):
    ./venv_jwt/bin/python3 agent_step5_ollama.py

Modo de verificacion SIN tocar Ollama (instancia el Agent, confirma
LiteLlm+tools sin invocar el Runner):
    ./venv_jwt/bin/python3 agent_step5_ollama.py --selftest
"""
import sys
import uuid

from google.adk.agents import Agent
from google.adk.models.lite_llm import LiteLlm
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types

from client import call_tool  # cliente mTLS+JWT del Paso 4

AGENT_ROLE = "logistics-agent"  # fijo por codigo -- no expuesto al LLM


def get_inventory_count(part_number: str) -> dict:
    """Consulta el inventario de un numero de parte a traves del MCP Server
    protegido por mTLS + JWT + role-check (Pasos 1-4). El rol del agente
    (logistics-agent) esta fijo por codigo, no es un parametro que el LLM
    elija."""
    resp = call_tool("get_inventory_count", role=AGENT_ROLE, params={"part_number": part_number})
    body = resp.json()
    print(f"[tool call ejecutado] get_inventory_count({part_number!r}) -> HTTP {resp.status_code}: {body}")
    return body


def check_stock_location(part_number: str) -> dict:
    """Consulta la ubicacion de deposito de un numero de parte a traves del
    MCP Server protegido por mTLS + JWT + role-check."""
    resp = call_tool("check_stock_location", role=AGENT_ROLE, params={"part_number": part_number})
    body = resp.json()
    print(f"[tool call ejecutado] check_stock_location({part_number!r}) -> HTTP {resp.status_code}: {body}")
    return body


logi_agent = Agent(
    name="logi_agent_lab_step5",
    model=LiteLlm(
        model="ollama_chat/qwen3.5:9b",
        num_ctx=8192,
        temperature=0.2,
        reasoning_effort="none",
    ),
    description="Agente logístico con acceso a herramientas de inventario vía MCP seguro",
    instruction=(
        "REGLA OBLIGATORIA, ANTES QUE NADA: para CUALQUIER pedido sobre "
        "inventario o ubicacion de stock, tenes que llamar a "
        "get_inventory_count o check_stock_location (la que corresponda) "
        "ANTES de responder. Nunca falta la llamada: generar una respuesta "
        "sin haber llamado la tool real es un error grave -equivale a "
        "inventar un dato de inventario que nunca se consulto.\n\n"
        "Solo usá herramientas de inventario. Si el MCP Server devuelve un "
        "error HTTP 403, eso es el role-check funcionando como se espera "
        "-no es un bug a evitar, reportalo tal cual.\n\n"
        "Despues de la llamada, respondé con SOLO el reporte final de "
        "abajo, completado con los valores reales devueltos por la tool. "
        "No narres tu razonamiento -devolve SOLO esta plantilla exacta con "
        "los valores sustituidos:\n\n"
        "Tool invocada: <get_inventory_count o check_stock_location>\n"
        "Resultado: <el resultado real devuelto por la tool>"
    ),
    tools=[get_inventory_count, check_stock_location],
)

_svc = InMemorySessionService()
_runner = Runner(agent=logi_agent, app_name="logi-agent-step5", session_service=_svc)


def ask_logi_agent(query: str) -> str:
    session = _svc.create_session_sync(app_name="logi-agent-step5", user_id="student",
                                        session_id=str(uuid.uuid4()))
    msg = types.Content(role="user", parts=[types.Part(text=query)])
    out = ""
    for event in _runner.run(user_id="student", session_id=session.id, new_message=msg):
        if event.content and event.content.parts:
            for part in event.content.parts:
                if getattr(part, "text", None):
                    out = part.text
    return out


def _selftest() -> None:
    assert logi_agent.name == "logi_agent_lab_step5"
    assert isinstance(logi_agent.model, LiteLlm)
    assert logi_agent.model.model == "ollama_chat/qwen3.5:9b"
    assert {t.__name__ for t in logi_agent.tools} == {"get_inventory_count", "check_stock_location"}
    print("[selftest] Agent instanciado con LiteLlm(ollama_chat/qwen3.5:9b) + tools "
          "mTLS/JWT -- OK (no se llamo a Ollama ni al server)")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        _selftest()
        sys.exit(0)

    print("=== Paso 5: agente ADK real consultando inventario via mTLS+JWT+role-check ===")
    print(ask_logi_agent("¿Cuánto stock hay del repuesto X-1000 y en qué depósito está?"))
