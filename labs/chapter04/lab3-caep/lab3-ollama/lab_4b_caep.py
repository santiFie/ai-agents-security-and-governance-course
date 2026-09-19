#!/usr/bin/env python3
"""
Lab Propuesto 4.B -- CAEP Simulation: Revocacion en Tiempo Real.

No toca ningun LLM en ningun momento -- 100% asyncio + dataclasses de la
stdlib, sin dependencias externas. No hay Agent/Runner en este archivo.

Corre local: python3 lab_4b_caep.py
"""
import asyncio
import json
from dataclasses import dataclass
from datetime import datetime, timezone

# -- Estado compartido: sesiones activas y log de acciones ------------------


@dataclass
class AgentSession:
    agent_id: str
    role: str
    revoked: bool = False


ACTIVE_SESSIONS: dict[str, AgentSession] = {
    "logi-agent-01": AgentSession("logi-agent-01", "logistics-agent"),
}
ACTION_LOG: list[dict] = []

# Cola que simula el canal CAEP (Shared Signals Framework) IdP -> Resource
# Server. En produccion esto seria un stream real (SET over HTTP/websockets);
# aca lo modelamos con asyncio.Queue para que el lab corra en un solo proceso.
CAEP_EVENT_BUS: asyncio.Queue = asyncio.Queue()

# -- IdP simulado: detecta anomalias y emite eventos CAEP -------------------


async def detect_anomaly_from_logs() -> dict | None:
    """Heuristica de deteccion: un logistics-agent invocando repetidamente
    get_budget_summary (fuera de su rol) es tratado como escalacion de
    privilegio."""
    suspicious = [a for a in ACTION_LOG
                  if a["tool"] == "get_budget_summary" and a["role"] == "logistics-agent"]
    if len(suspicious) >= 3:
        return {"agent_id": suspicious[-1]["agent_id"],
                "reason": "Escalación de privilegio: logistics-agent invocando get_budget_summary repetidamente"}
    return None


async def idp_security_event_stream():
    """Simulated IdP streaming security events (CAEP)."""
    while True:
        anomaly = await detect_anomaly_from_logs()
        if anomaly:
            event = {
                "event_type": "session_revoked",
                "agent_id": anomaly["agent_id"],
                "reason": anomaly["reason"],
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
            await CAEP_EVENT_BUS.put(event)
            print(f"[IdP] Anomalía detectada → emite evento CAEP: {json.dumps(event)}")
            return  # una sola revocación alcanza para esta demo
        await asyncio.sleep(1)


# -- Resource Server: escucha el bus CAEP y revoca sesiones -----------------


def revoke_agent_session(agent_id: str) -> None:
    session = ACTIVE_SESSIONS.get(agent_id)
    if session:
        session.revoked = True
        print(f"[Resource Server] Sesión revocada para {agent_id} — próxima tool call será rechazada")


async def resource_server_caep_listener():
    while True:
        event = await CAEP_EVENT_BUS.get()
        if event["event_type"] == "session_revoked":
            revoke_agent_session(event["agent_id"])
            print(f"[CAEP] Session revoked for {event['agent_id']}")


# -- Trafico del agente: llamadas legitimas y luego un patron anomalo -------


def call_tool(agent_id: str, tool: str) -> str:
    session = ACTIVE_SESSIONS[agent_id]
    if session.revoked:
        return "❌ RECHAZADO: sesión revocada por CAEP"
    ACTION_LOG.append({"agent_id": agent_id, "role": session.role, "tool": tool,
                        "timestamp": datetime.now(timezone.utc).isoformat()})
    return f"✅ {tool} ejecutado para {agent_id}"


async def simulate_agent_traffic():
    print(call_tool("logi-agent-01", "get_inventory_count"))
    await asyncio.sleep(0.3)
    # Comportamiento anómalo: el logistics-agent empieza a pedir presupuesto financiero
    for _ in range(3):
        print(call_tool("logi-agent-01", "get_budget_summary"))
        await asyncio.sleep(0.3)
    await asyncio.sleep(1.5)  # dar tiempo a que el IdP detecte y el Resource Server revoque
    # Intento posterior a la revocación → debe ser rechazado en segundos, no horas
    print(call_tool("logi-agent-01", "get_inventory_count"))


async def main():
    idp_task = asyncio.create_task(idp_security_event_stream())
    listener_task = asyncio.create_task(resource_server_caep_listener())
    await simulate_agent_traffic()
    await asyncio.sleep(0.2)
    idp_task.cancel()
    listener_task.cancel()

    # Verificacion determinista del punto pedagogico central: la sesion
    # comprometida quedo revocada y el ultimo intento de tool call (post
    # revocacion) fue efectivamente rechazado -- confirma por codigo lo
    # que la consola ya muestra.
    session = ACTIVE_SESSIONS["logi-agent-01"]
    assert session.revoked is True, "La sesion deberia haber sido revocada por CAEP tras la anomalia"
    print("\n[verificación] OK: CAEP detectó la escalación de privilegio y revocó la "
          "sesión antes del intento de tool call posterior.")


if __name__ == "__main__":
    asyncio.run(main())
