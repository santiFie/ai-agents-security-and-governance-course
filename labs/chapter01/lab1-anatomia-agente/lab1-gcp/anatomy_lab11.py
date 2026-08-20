#!/usr/bin/env python3
"""
Lab Propuesto 1.1: Anatomia de un Agente — version Gemini via Vertex AI.

Adaptado del "Codigo esqueleto" del Lab Propuesto 1.1 (ch01-labs.md, lineas
34-109). Reproduce sin cambios el nucleo pedagogico: un agente ReAct minimo
con 2 tools instrumentadas (read_file, write_file) que loguean cada paso del
ciclo Planificador -> Ejecutor -> Herramientas -> Memoria, para que el
estudiante pueda leer el trace completo y mapearlo a la anatomia del agente.

Esta es la version GCP de labs/ch01-lab1-ollama/anatomy_lab11.py (que corre
contra qwen3.5:9b local via Ollama). El nucleo pedagogico y todas las mejoras
de diseno encontradas durante esa migracion (evidencia de tool-calling por
print, instruccion imperativa) se mantienen tal cual -no son "fixes de
Ollama", son buenas practicas generales de diseno de agentes que tambien
aplican con Gemini.

Cambio de esta version: el agente usa `model=GEMINI_MODEL` (string plano,
sin wrapper LiteLlm) -ADK resuelve el backend real (Vertex AI vs AI Studio)
en base a las variables de entorno GOOGLE_GENAI_USE_VERTEXAI /
GOOGLE_CLOUD_PROJECT / GOOGLE_CLOUD_LOCATION (Vertex AI) o GOOGLE_API_KEY
(AI Studio), definidas al momento de correr el lab -no hardcodeadas aca. Se
quitaron los parametros especificos de Ollama (num_ctx, reasoning_effort);
Gemini no es un modelo "thinking" en el sentido que motivaba
reasoning_effort="none" con qwen3.5, asi que ese parametro no tiene
equivalente ni hace falta.

La instruccion imperativa ("REGLA OBLIGATORIA...") se mantiene: aunque el
hallazgo que la motivo (Lab 11.A, qwen3.5:9b a veces no llamaba la tool y
alucinaba un resultado) fue especifico del modelo local, un mandato
imperativo explicito de tool-calling no hace dano con Gemini y sigue siendo
buena practica general -no depender de que el modelo "elija bien" cuando el
codigo puede exigirlo.

Requiere: google-adk, y credenciales de Vertex AI (ADC de gcloud con
GOOGLE_CLOUD_PROJECT/GOOGLE_CLOUD_LOCATION exportadas, o GOOGLE_GENAI_USE_
VERTEXAI=true) o una GOOGLE_API_KEY de AI Studio -segun se decida al correr
el lab.

Modo de verificacion sin llamadas reales (tools read_file/write_file +
construccion del Agent, sin invocar Runner/Vertex AI):
    python3 anatomy_lab11.py --selftest
"""
import os
import sys
import uuid

from google.adk.agents import Agent
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types

# Modelo Gemini via ADK -- string plano, ADK decide Vertex AI vs AI Studio
# segun las env vars definidas al correr el lab (ver docstring del modulo).
# El proyecto GCP todavia no esta decidido: no hardcodear ningun project id.
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.0-flash")


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
    model=GEMINI_MODEL,
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
    SIN invocar el LLM/Vertex AI: escribe y lee un archivo temporal
    directamente en Python (mismas funciones que usaria el agente como
    tools), y confirma que Agent(...) se instancia con el modelo Gemini sin
    tocar la red -construir el Agent es solo construccion de objeto, ADK no
    dispara ninguna llamada al resolver el string del modelo."""
    import tempfile

    print("[selftest] --- Tools read_file/write_file (sin LLM) ---")
    tmp_path = tempfile.mktemp(suffix=".txt")
    w = write_file(tmp_path, "Hola ADK selftest")
    assert w == {"status": "written", "path": tmp_path}, w
    content = read_file(tmp_path)
    assert content == "Hola ADK selftest", content
    os.remove(tmp_path)
    print(f"[selftest] OK: write_file/read_file funcionan de punta a punta ({w})")

    print("\n[selftest] --- Construccion del Agent (sin Runner/Vertex AI) ---")
    assert agent.name == "anatomy_lab"
    assert len(agent.tools) == 2
    assert agent.model == GEMINI_MODEL
    print(
        f"[selftest] OK: Agent '{agent.name}' instanciado con "
        f"{len(agent.tools)} tools y modelo '{agent.model}', sin "
        f"invocar Vertex AI/AI Studio."
    )

    print("\n[selftest] OK: nucleo determinista de Lab 1.1 verificado de punta a punta.")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        _self_test()
        sys.exit(0)

    print("=" * 60)
    print("Lab 1.1: Anatomia de un Agente")
    print(f"Gemini ({GEMINI_MODEL}) via Vertex AI / AI Studio")
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
