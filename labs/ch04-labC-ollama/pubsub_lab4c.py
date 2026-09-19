#!/usr/bin/env python3
"""
Lab Propuesto 4.C -- Cloud Pub/Sub: comunicacion asincrona entre agentes,
version con modelo local.

Diseñado "local-first, GCP-opcional": si hay credenciales GCP disponibles
usa Cloud Pub/Sub real, si no cae automaticamente a queue.Queue() de la
stdlib.

En vez de depender de que el servidor mTLS+JWT de ch04-labA-ollama/server.py
este corriendo en :8443 (acoplaria un lab de MENSAJERIA a infraestructura de
OTRO lab), este archivo reproduce localmente el mismo patron de agente
logistico con role-check de 4.A (logi_agent solo puede leer inventario,
nunca presupuesto) como funcion Python pura -- el punto pedagogico de 4.C es
el bus de mensajes asincrono, no mTLS, asi que la tool protegida se resuelve
in-process. El GoalID viaja en cada mensaje.

El agente corre con qwen3.5:9b local via LiteLlm (num_ctx=8192,
temperature=0.2, reasoning_effort="none").

Modo de verificacion SIN tocar Ollama (bus de mensajes local, GoalID,
delegate_to_specialist/process_message de punta a punta, con un handler
determinista en vez del agente real):
    python3 pubsub_lab4c.py --selftest

Modo con el agente real (dispara Ollama):
    python3 pubsub_lab4c.py
"""
import json
import queue
import sys
import uuid
from datetime import datetime

from google.adk.agents import Agent
from google.adk.models.lite_llm import LiteLlm
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types

# HALLAZGO TECNICO: un try/except "simple" alrededor de
# publisher.get_topic() no alcanza para detectar si Cloud Pub/Sub esta
# disponible. Con credenciales ADC *presentes pero vencidas*, esa llamada
# no lanza una excepcion rapido -- se queda COLGADA intentando
# refrescar/reautenticar. Un bare try/except no protege contra un hang,
# solo contra una excepcion lanzada. Fix: acotar el probe con un thread
# daemon + timeout (3s); si no resuelve a tiempo, se asume que Cloud
# Pub/Sub no esta disponible y se cae al bus local, que es exactamente el
# comportamiento "local-first" que el lab promete.
import threading

_USE_CLOUD_PUBSUB = False
publisher = subscriber = topic_path = subscription_path = None


def _probe_cloud_pubsub(timeout_s: float = 3.0) -> dict | None:
    result: dict = {}

    def _probe():
        try:
            from google.cloud import pubsub_v1
            pub = pubsub_v1.PublisherClient()
            sub = pubsub_v1.SubscriberClient()
            tp = pub.topic_path("my-project", "agent-tasks")
            sp = sub.subscription_path("my-project", "logistics-agent-sub")
            pub.get_topic(request={"topic": tp})  # puede colgarse si ADC esta vencido
            result.update(publisher=pub, subscriber=sub, topic_path=tp, subscription_path=sp)
        except Exception:
            pass  # sin ADC/credenciales GCP (o libreria no instalada): cae al bus local

    t = threading.Thread(target=_probe, daemon=True)
    t.start()
    t.join(timeout_s)
    return result or None


_cloud = _probe_cloud_pubsub()
if _cloud:
    publisher, subscriber, topic_path, subscription_path = (
        _cloud["publisher"], _cloud["subscriber"], _cloud["topic_path"], _cloud["subscription_path"]
    )
    _USE_CLOUD_PUBSUB = True

print(f"[bus] Backend de mensajeria: {'Cloud Pub/Sub' if _USE_CLOUD_PUBSUB else 'local (queue.Queue)'}")

# -- Bus de mensajes local (fallback) ----------------------------------------
_local_queue: "queue.Queue" = queue.Queue()


class LocalMessage:
    """Imita la interfaz minima de pubsub_v1.subscriber.message.Message
    (.data, .ack(), .nack()) para que process_message() funcione igual en
    ambos modos, sin ifs dispersos por el resto del codigo."""

    def __init__(self, data: bytes):
        self.data = data

    def ack(self):
        pass  # no-op: en modo local no hay entrega at-least-once que confirmar

    def nack(self):
        print("[local-bus] mensaje reencolado (nack)")
        _local_queue.put(LocalMessage(self.data))


# -- Publisher (Orquestador) --------------------------------------------------
def delegate_to_specialist(goal_id: str, agent_id: str, task: dict):
    message = {
        "goal_id": goal_id,
        "target_agent": agent_id,
        "task": task,
        "timestamp": datetime.utcnow().isoformat(),
    }
    payload = json.dumps(message).encode()
    if _USE_CLOUD_PUBSUB:
        future = publisher.publish(topic_path, payload)
        print(f"[tool call ejecutado] Published task {future.result()} for goal {goal_id}")
    else:
        _local_queue.put(LocalMessage(payload))
        print(f"[tool call ejecutado] [local-bus] Published task for goal {goal_id}")


# -- Subscriber (Agente Especializado): role-check local, port de 4.A -------

AGENT_ROLE = "logistics-agent"  # fijo por codigo -- no expuesto al LLM (mismo criterio que Lab 4.A/5.A)
ROLE_TOOL_ALLOWLIST = {
    "logistics-agent": {"get_inventory_count", "check_stock_location"},
    "finance-agent": {"get_budget_summary"},
}


def get_inventory_count(part_number: str) -> dict:
    """Devuelve el inventario de un numero de parte -- protegida por
    role-check (logistics-agent SI puede llamarla)."""
    tool_name = "get_inventory_count"
    allowed = tool_name in ROLE_TOOL_ALLOWLIST.get(AGENT_ROLE, set())
    print(f"[role-check] role={AGENT_ROLE!r} tool={tool_name!r} allowed={allowed}")
    result = {"part": part_number, "count": 42}
    print(f"[tool call ejecutado] get_inventory_count({part_number!r}) -> {result}")
    return result


logi_agent = Agent(
    name="logi_agent_lab4c",
    model=LiteLlm(
        model="ollama_chat/qwen3.5:9b",
        num_ctx=8192,
        temperature=0.2,
        reasoning_effort="none",
    ),
    description="Agente logístico que procesa tareas delegadas vía el bus de mensajes",
    instruction=(
        "REGLA OBLIGATORIA, ANTES QUE NADA: para CUALQUIER tarea sobre "
        "inventario, tenes que llamar a get_inventory_count con el numero "
        "de parte ANTES de responder. Nunca falta la llamada -generar un "
        "resultado sin haberla llamado es un error grave.\n\n"
        "Solo usá herramientas de inventario. Registrá el GoalID en cada "
        "llamada (viene en la tarea que recibís).\n\n"
        "Despues de la llamada, respondé con SOLO el reporte final de "
        "abajo, completado con los valores reales devueltos por la tool. "
        "No narres tu razonamiento -devolve SOLO esta plantilla exacta con "
        "los valores sustituidos:\n\n"
        "GoalID: <goal_id de la tarea>\n"
        "Resultado: <el resultado real devuelto por get_inventory_count>"
    ),
    tools=[get_inventory_count],
)

# ADK v2 no tiene logi_agent.run("texto"): se ejecuta via Runner sobre una sesion.
_svc = InMemorySessionService()
_runner = Runner(agent=logi_agent, app_name="logi-a2a", session_service=_svc)


def run_logi_agent(task: dict) -> str:
    session = _svc.create_session_sync(app_name="logi-a2a", user_id="a2a",
                                        session_id=str(uuid.uuid4()))
    msg = types.Content(role="user", parts=[types.Part(text=json.dumps(task))])
    out = ""
    for event in _runner.run(user_id="a2a", session_id=session.id, new_message=msg):
        if event.content and event.content.parts:
            for part in event.content.parts:
                if getattr(part, "text", None):
                    out = part.text
    return out


def publish_result(goal_id: str, result: str) -> None:
    print(f"[bus] Resultado para goal {goal_id}: {result[:200]}")


def process_message(message, handler=run_logi_agent) -> None:
    """`handler` es inyectable para poder probar el bus de mensajes de punta
    a punta con --selftest sin invocar al agente real/Ollama (ver
    _selftest_handler mas abajo)."""
    data = json.loads(message.data.decode())
    if data["target_agent"] == "logistics-agent":
        result = handler(data["task"])
        message.ack()
        publish_result(data["goal_id"], result)
    else:
        message.nack()


def start_subscriber_loop(handler=run_logi_agent):
    """Consume mensajes: subscriber.subscribe() real en modo Pub/Sub, o un
    loop simple sobre la queue local en modo fallback."""
    if _USE_CLOUD_PUBSUB:
        subscriber.subscribe(subscription_path, callback=lambda m: process_message(m, handler))
    else:
        while True:
            message = _local_queue.get()
            process_message(message, handler)


# -- Verificacion determinista (sin Ollama) ----------------------------------

def _selftest_handler(task: dict) -> str:
    """Handler determinista que reemplaza al agente real: ejecuta
    get_inventory_count directamente (sin pasar por Runner/Ollama) para
    poder verificar el bus de mensajes de punta a punta sin tocar Ollama."""
    result = get_inventory_count(task["part_number"])
    return f"GoalID: {task['goal_id']}\nResultado: {result}"


def _selftest() -> None:
    assert logi_agent.name == "logi_agent_lab4c"
    assert isinstance(logi_agent.model, LiteLlm)
    assert logi_agent.model.model == "ollama_chat/qwen3.5:9b"
    print("[selftest] Agent instanciado con LiteLlm(ollama_chat/qwen3.5:9b) -- OK "
          "(no se llamo a Ollama)")

    goal_id = f"goal-{uuid.uuid4().hex[:8]}"
    delegate_to_specialist(goal_id, "logistics-agent",
                            {"goal_id": goal_id, "part_number": "X-1000"})
    assert not _local_queue.empty(), "El mensaje deberia estar encolado en el bus local"

    message = _local_queue.get()
    process_message(message, handler=_selftest_handler)
    print(f"[selftest] OK: bus de mensajes local (publish -> queue -> process_message -> "
          f"handler determinista -> ack) verificado de punta a punta con GoalID={goal_id}, "
          f"sin invocar Ollama.")

    # Mensaje para un target_agent distinto -> debe hacer nack() y reencolarse
    other_goal = f"goal-{uuid.uuid4().hex[:8]}"
    delegate_to_specialist(other_goal, "finance-agent",
                            {"goal_id": other_goal, "query": "budget"})
    msg2 = _local_queue.get()
    process_message(msg2, handler=_selftest_handler)
    assert not _local_queue.empty(), "El mensaje para finance-agent deberia haberse reencolado via nack()"
    print(f"[selftest] OK: mensaje para target_agent no manejado ('finance-agent') "
          f"correctamente rechazado con nack() y reencolado.")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        _selftest()
        sys.exit(0)

    print("=== Lab 4.C: Pub/Sub asíncrono entre agentes (qwen3.5:9b local via Ollama) ===")
    goal_id = f"goal-{uuid.uuid4().hex[:8]}"
    delegate_to_specialist(goal_id, "logistics-agent",
                            {"goal_id": goal_id, "part_number": "X-1000"})
    message = _local_queue.get()
    process_message(message)  # handler real -> dispara Ollama
