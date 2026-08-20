#!/usr/bin/env python3
"""
Lab Propuesto 1.1: Anatomia de un Agente — version con modelo local.

Adaptado del "Codigo esqueleto" del Lab Propuesto 1.1 (ch01-labs.md, lineas
34-109). Reproduce sin cambios el nucleo pedagogico: un agente ReAct minimo
con 2 tools instrumentadas (read_file, write_file) que loguean cada paso del
ciclo Planificador -> Ejecutor -> Herramientas -> Memoria, para que el
estudiante pueda leer el trace completo y mapearlo a la anatomia del agente.

Cambio respecto al original: el agente pasa de model="gemini-2.0-flash" a
qwen3.5:9b corriendo localmente en Ollama, via el wrapper LiteLlm de ADK -
misma config verificada en los labs de capitulos 5/8/11 (num_ctx=8192,
temperature=0.2, reasoning_effort="none"; ver el comentario largo en
labs/ch08-lab1-ollama/red_team_lab81.py para el diagnostico completo de por
que reasoning_effort="none" hace falta con un modelo "thinking" como qwen3.5).

Segundo cambio (aplicado preventivamente, lecciones de ch08/ch11): la
instruccion original del libro ("Completa las tareas solicitadas usando las
herramientas disponibles") es meramente descriptiva. En Lab 11.A encontramos
que con una instruccion asi, qwen3.5:9b a veces NUNCA llama la tool y
alucina un resultado plausible (p.ej. "escribi el archivo" sin haber llamado
write_file). Este lab es justamente el que ensena a leer el trace de tool
calls -si el modelo no llama la tool, no hay trace que leer y el punto
pedagogico se pierde. Por eso la instruccion abajo antepone una REGLA
OBLIGATORIA en mayusculas exigiendo la llamada real antes de responder.

Requiere: google-adk, litellm, y Ollama corriendo con qwen3.5:9b ya
descargado (ollama pull qwen3.5:9b).

Modo de verificacion sin LLM (tools read_file/write_file + construccion del
Agent, sin invocar Runner/Ollama):
    python3 anatomy_lab11.py --selftest
"""
import os
import sys
import uuid

from google.adk.agents import Agent
from google.adk.models.lite_llm import LiteLlm
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types


# ── Herramientas instrumentadas (funciones planas — ADK v2 no usa @tool) ───
def read_file(path: str) -> str:
    """Lee un archivo del sistema local."""
    print(f"[EJECUTOR] Tool call: read_file({path})")  # surface de ataque: filesystem
    with open(path, "r") as f:
        content = f.read()
    print(f"[EJECUTOR] Tool result: {len(content)} chars")
    return content


def write_file(path: str, content: str) -> dict:
    """Escribe contenido en un archivo."""
    print(f"[EJECUTOR] Tool call: write_file({path})")  # surface de ataque: filesystem write
    with open(path, "w") as f:
        f.write(content)
    result = {"status": "written", "path": path}
    # Print de evidencia tambien en write_file (el libro solo lo tenia en
    # read_file) — es la tool cuyo efecto (archivo creado) hay que poder
    # confirmar por evidencia, no por el texto que narre el agente despues.
    print(f"[EJECUTOR] Tool result: {result}")
    return result


# ── Agente con logging de componentes ───────────────────────────────────
agent = Agent(
    name="anatomy_lab",
    # num_ctx=8192, temperature=0.2, reasoning_effort="none": misma config
    # verificada en Lab 8.1/8.2/11.A. qwen3.5 es un modelo "thinking"; sin
    # reasoning_effort="none" (que fuerza think=False en Ollama via litellm)
    # se queda narrando su razonamiento y nunca cierra la respuesta final.
    model=LiteLlm(
        model="ollama_chat/qwen3.5:9b",
        num_ctx=8192,
        temperature=0.2,
        reasoning_effort="none",
    ),
    description="Agente de laboratorio para analisis de anatomia.",
    instruction=(
        "REGLA OBLIGATORIA, ANTES QUE NADA: para CUALQUIER pedido que "
        "implique leer o escribir un archivo, tenes que llamar a la tool "
        "correspondiente (read_file o write_file) con los parametros reales "
        "ANTES de responder. Nunca falta la llamada: no importa cuan simple "
        "u obvio parezca el pedido, siempre invocas la tool primero. "
        "Describir el contenido de un archivo, o confirmar que algo se "
        "escribio, sin haber llamado realmente a la tool es un error grave "
        "-equivale a inventar un resultado que nunca ocurrio.\n\n"
        "Completa las tareas solicitadas usando las herramientas "
        "disponibles."
    ),
    tools=[read_file, write_file],
)


# ── Ejecucion via Runner (patron canonico ADK v2; no existe agent.run("texto")) ─
_session_service = InMemorySessionService()
_runner = Runner(agent=agent, app_name="anatomy-lab", session_service=_session_service)


def run_task(task: str) -> str:
    session = _session_service.create_session_sync(
        app_name="anatomy-lab", user_id="lab", session_id=str(uuid.uuid4())
    )
    msg = types.Content(role="user", parts=[types.Part(text=task)])
    final = ""
    for event in _runner.run(user_id="lab", session_id=session.id, new_message=msg):
        if event.content and event.content.parts:
            for part in event.content.parts:
                # [PLANIFICADOR] el modelo emite function_call; [EJECUTOR] la corre
                if getattr(part, "function_call", None):
                    print(f"[PLANIFICADOR->EJECUTOR] {part.function_call.name}")
                if getattr(part, "text", None):
                    final = part.text
    return final


def _self_test() -> None:
    """Verifica las tools (read_file/write_file) y la construccion del Agent
    SIN invocar el LLM/Ollama: escribe y lee un archivo temporal directamente
    en Python (mismas funciones que usaria el agente como tools), y confirma
    que Agent(...) se instancia con el modelo local sin tocar la red."""
    import tempfile

    print("[selftest] --- Tools read_file/write_file (sin LLM) ---")
    tmp_path = tempfile.mktemp(suffix=".txt")
    w = write_file(tmp_path, "Hola ADK selftest")
    assert w == {"status": "written", "path": tmp_path}, w
    content = read_file(tmp_path)
    assert content == "Hola ADK selftest", content
    os.remove(tmp_path)
    print(f"[selftest] OK: write_file/read_file funcionan de punta a punta ({w})")

    print("\n[selftest] --- Construccion del Agent (sin Runner/Ollama) ---")
    assert agent.name == "anatomy_lab"
    assert len(agent.tools) == 2
    assert isinstance(agent.model, LiteLlm)
    assert agent.model.model == "ollama_chat/qwen3.5:9b"
    print(
        f"[selftest] OK: Agent '{agent.name}' instanciado con "
        f"{len(agent.tools)} tools y modelo '{agent.model.model}', sin "
        f"invocar Ollama."
    )

    print("\n[selftest] OK: nucleo determinista de Lab 1.1 verificado de punta a punta.")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        _self_test()
        sys.exit(0)

    print("=" * 60)
    print("Lab 1.1: Anatomia de un Agente")
    print("qwen3.5:9b local via Ollama")
    print("=" * 60)

    print("\n=== TAREA 1: escribir y luego leer un archivo ===")
    resultado_1 = run_task(
        "Escribi el texto 'Hola ADK' en el archivo /tmp/lab_anatomy.txt "
        "y luego leelo para confirmar el contenido."
    )
    print("Respuesta final del agente:", resultado_1)

    print("\n=== TAREA 2: leer el archivo generado y contar caracteres ===")
    resultado_2 = run_task(
        "Lee el archivo /tmp/lab_anatomy.txt y decime cuantos caracteres tiene."
    )
    print("Respuesta final del agente:", resultado_2)

    # Analisis del trace (a completar por el estudiante a partir de los prints):
    # - [PLANIFICADOR->EJECUTOR] <tool>: marca el momento en que el modelo
    #   decide que herramienta invocar (componente Planificador del ciclo ReAct).
    # - [EJECUTOR] Tool call / Tool result: dentro de read_file/write_file,
    #   muestra la ejecucion real de la tool (componentes Ejecutor + Herramientas).
    # - [MEMORIA]: el historial de la sesion (session.id, session_service)
    #   mantiene el contexto entre turnos -inspeccionalo para ver los eventos
    #   acumulados.
    # - Punto de inyeccion: todo lo que devuelve read_file() vuelve al modelo
    #   como parte de la conversacion; si ese archivo contuviera instrucciones
    #   maliciosas, el modelo podria interpretarlas como ordenes (mismo
    #   mecanismo que el Confused Deputy del Lab 1.2).
