#!/usr/bin/env python3
"""
Lab Propuesto 1.3: Mapeo de Superficie de Ataque — Herramienta de Auditoria,
version GCP / Gemini.

Adaptado del "Codigo esqueleto" del Lab Propuesto 1.3 (ch01-labs.md, lineas
239-280). A diferencia de los Labs 1.1 y 1.2, este NO invoca ningun LLM: es
introspeccion estatica via `inspect` sobre `agent.tools` / `agent.model`. Por
eso corre de punta a punta sin tocar Vertex AI, sin GPU y sin red -es la
unica pieza de este capitulo que no necesita ningun tipo de credencial.

Esta es la version GCP de labs/ch01-lab3-ollama/attack_surface_lab13.py.
Diferencia real de comportamiento respecto a esa version (no un simple
find/replace): con Gemini, `agent.model` vuelve a ser un string plano
("gemini-2.0-flash") en vez del objeto LiteLlm que envolvia a Ollama. El
helper `_extract_model_name()` (agregado en la migracion a Ollama para
soportar el caso LiteLlm sin romper con TypeError) se mantiene tal cual --
sigue siendo codigo defensivo razonable, útil si algun agente futuro vuelve
a usar un wrapper -- pero con un string plano toma la rama `isinstance(m,
str)` y listo.

Cambio de comportamiento real: "gemini-2.0-flash" SI contiene la substring
"flash", asi que `map_attack_surface()` ahora agrega el canal de input
"image" para ambos agentes de demostracion (a diferencia de la version
Ollama, donde "ollama_chat/qwen3.5:9b" no activaba esa condicion). Esto NO
es un bug: es la deteccion de multimodalidad funcionando como se espera --
Gemini 2.0 Flash es multimodal de verdad, qwen3.5:9b via Ollama en ese setup
no lo era. El `attack_surface_score` cambia en consecuencia (ver
`_verify_reports` y el .md).

Requiere: google-adk (no requiere litellm; esta version no usa LiteLlm en
ningun punto).

Ejecucion (no toca Vertex AI, no requiere flag especial ni credenciales):
    python3 attack_surface_lab13.py
"""
import inspect
import json
import os

from google.adk.agents import Agent

# Modelo Gemini via ADK -- string plano, ADK decide Vertex AI vs AI Studio
# segun las env vars definidas al correr el lab. Para ESTE lab en particular
# el nombre del modelo es un dato mas a inspeccionar -- nunca se invoca.
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.0-flash")


def _extract_model_name(agent: Agent) -> str:
    """Devuelve un string identificando el modelo del agente, sea un string
    plano (el caso normal con Gemini nativo, p.ej. 'gemini-2.0-flash') o un
    wrapper como LiteLlm (usado en la version Ollama de este seminario para
    correr contra un modelo local). El original del libro asumia siempre un
    string y rompia con TypeError al hacer `"flash" in model_name` sobre un
    objeto no iterable de esa forma -- este helper se mantiene por ser
    codigo defensivo de proposito general, no una muleta especifica de
    Ollama: sigue siendo correcto (y gratis) soportar ambos casos aunque
    esta version en particular solo use el string plano."""
    m = getattr(agent, "model", "unknown")
    if isinstance(m, str):
        return m
    return getattr(m, "model", str(m))


# Peso por nivel de riesgo para el scoring de tools (ver "Hallazgo 2" en
# attack_surface_lab13.md): la version original del libro contaba tools con
# un peso fijo de 3 cada una, sin mirar el campo "risk" que
# map_attack_surface() ya calculaba -por eso un agente de solo
# lectura/escritura de archivos (MEDIUM/MEDIUM) y uno que emite creditos y
# borra cuentas (HIGH/HIGH) terminaban con el mismo attack_surface_score
# pese a tener perfiles de riesgo opuestos. Ver la variante -ollama de este
# mismo lab para los valores verificados en ejecucion real (la logica es
# identica, esta funcion no invoca ningun LLM en ningun momento).
_TOOL_RISK_WEIGHT = {"HIGH": 5, "MEDIUM": 2}


def map_attack_surface(agent: Agent) -> dict:
    """
    Mapea la superficie de ataque de un agente ADK.
    Retorna un reporte estructurado con las 5 categorias.
    """
    surface = {
        "input_channels": [],       # Categoria 1
        "memory_stores": [],        # Categoria 2
        "tool_interfaces": [],      # Categoria 3
        "inter_agent_channels": [], # Categoria 4
        "training_data": [],        # Categoria 5
    }

    for tool_func in (agent.tools or []):
        name = getattr(tool_func, "__name__", str(tool_func))
        params = list(inspect.signature(tool_func).parameters.keys()) if callable(tool_func) else []
        risk = "HIGH" if any(k in name for k in ["credit", "transfer", "delete", "admin"]) else "MEDIUM"
        surface["tool_interfaces"].append({"name": name, "parameters": params, "risk": risk})

    model_name = _extract_model_name(agent)
    surface["input_channels"].append({"type": "text", "model": model_name})
    if "flash" in model_name or "pro" in model_name:
        surface["input_channels"].append({"type": "image", "model": model_name})

    if hasattr(agent, "memory") and agent.memory:
        surface["memory_stores"].append({"type": "long_term"})
    else:
        surface["memory_stores"].append({"type": "session_only"})

    tools_score = sum(
        _TOOL_RISK_WEIGHT.get(t["risk"], 2) for t in surface["tool_interfaces"]
    )
    surface["attack_surface_score"] = round(
        tools_score
        + len(surface["input_channels"]) * 2
        + len(surface["memory_stores"]) * 1.5,
        1,
    )

    return surface


# ── Agentes de demostracion (solo se instancian, nunca se invocan -no toca
#    Vertex AI). Reproducen el perfil de riesgo de los agentes de Lab 1.1
#    (bajo riesgo: filesystem local) y Lab 1.2 (alto riesgo: emision de
#    creditos + una tool administrativa extra para ilustrar el filtro por
#    keyword). ──────────────────────────────────────────────────────────
def read_file(path: str) -> str:
    """Lee un archivo del sistema local."""
    with open(path, "r") as f:
        return f.read()


def write_file(path: str, content: str) -> dict:
    """Escribe contenido en un archivo."""
    with open(path, "w") as f:
        f.write(content)
    return {"status": "written", "path": path}


def issue_credit(customer_id: str, amount: float) -> dict:
    """Emite un credito a la cuenta de un cliente."""
    return {"issued": amount, "customer": customer_id}


def delete_user_account(user_id: str) -> dict:
    """Elimina una cuenta de usuario del sistema (accion administrativa)."""
    return {"deleted": user_id}


benign_agent = Agent(
    name="anatomy_demo_agent",
    description="Agente de bajo riesgo: solo lee/escribe archivos locales (perfil Lab 1.1).",
    model=GEMINI_MODEL,
    instruction="Completa las tareas solicitadas usando las herramientas disponibles.",
    tools=[read_file, write_file],
)

high_risk_agent = Agent(
    name="finance_admin_agent",
    description="Agente de alto riesgo: emite creditos y administra cuentas (perfil Lab 1.2).",
    model=GEMINI_MODEL,
    instruction="Procesa pedidos financieros y administrativos.",
    tools=[issue_credit, delete_user_account],
)


def _verify_reports(benign_report: dict, high_risk_report: dict) -> None:
    """Chequeos deterministas sobre los reportes generados: confirman que la
    clasificacion de riesgo y el scoring funcionan como se espera, no solo
    que el script no exploto."""
    assert all(t["risk"] == "MEDIUM" for t in benign_report["tool_interfaces"]), (
        "read_file/write_file no deberian clasificar como HIGH risk"
    )
    assert any(t["risk"] == "HIGH" for t in high_risk_report["tool_interfaces"]), (
        "issue_credit deberia clasificar como HIGH risk (contiene 'credit')"
    )
    assert any(t["name"] == "delete_user_account" and t["risk"] == "HIGH" for t in high_risk_report["tool_interfaces"]), (
        "delete_user_account deberia clasificar como HIGH risk (contiene 'delete'/'admin')"
    )
    # FORMULA PONDERADA POR RIESGO: con la version original (peso fijo de 3
    # por tool, sin mirar "risk"), un agente de solo lectura/escritura de
    # archivos (MEDIUM) y uno que emite creditos y borra cuentas (HIGH)
    # terminaban con el MISMO score -11.5 en este entorno (con Gemini,
    # "flash" agrega el canal "image" a AMBOS agentes por igual, asi que el
    # empate persistia igual que en la version Ollama, solo cambiaba el
    # numero). Con _TOOL_RISK_WEIGHT (HIGH=5, MEDIUM=2), ahora si distinguen:
    # benign_agent -> 2*2 + 2*2 + 1*1.5 = 9.5; high_risk_agent -> 2*5 + 2*2 +
    # 1*1.5 = 15.5 (la variante Ollama da 7.5/13.5 -sin el canal "image"
    # extra que "flash" agrega solo en esta variante GCP). Se asserta que el
    # de mayor riesgo tenga mayor score, no solo que sean distintos.
    assert high_risk_report["attack_surface_score"] > benign_report["attack_surface_score"], (
        "con la formula ponderada por risk, high_risk_agent deberia tener "
        "un attack_surface_score mayor que benign_agent -si dan igual o al "
        "reves, revisar _TOOL_RISK_WEIGHT en map_attack_surface()"
    )
    assert any(t["risk"] == "HIGH" for t in high_risk_report["tool_interfaces"]) and all(
        t["risk"] == "MEDIUM" for t in benign_report["tool_interfaces"]
    ), "precondicion del hallazgo: los perfiles de riesgo deben ser realmente distintos"
    for report in (benign_report, high_risk_report):
        assert report["input_channels"][0]["model"] == GEMINI_MODEL, (
            "_extract_model_name deberia leer el string plano del modelo Gemini"
        )
        # Gemini 2.0 Flash SI contiene "flash" -- a diferencia de la version
        # Ollama (donde "ollama_chat/qwen3.5:9b" no activaba esta condicion
        # y el canal "image" nunca se agregaba), aca se espera 2 canales:
        # "text" + "image". Este es un cambio de comportamiento REAL
        # respecto a la version Ollama, no un ajuste cosmetico.
        assert len(report["input_channels"]) == 2, (
            f"con model={GEMINI_MODEL!r} (contiene 'flash'), map_attack_surface "
            "deberia agregar el canal 'image' ademas de 'text'"
        )
    print("[verify] OK: clasificacion de riesgo, canal de input y scoring se comportan como se espera.")


if __name__ == "__main__":
    print("=" * 70)
    print("Lab 1.3: Mapeo de Superficie de Ataque")
    print("(introspeccion estatica -no invoca ningun LLM)")
    print("=" * 70)

    reports = {}
    for label, agent in [
        ("benign_agent (read/write archivo -perfil Lab 1.1)", benign_agent),
        ("high_risk_agent (credito + admin -perfil Lab 1.2)", high_risk_agent),
    ]:
        print(f"\n--- REPORTE: {label} ---")
        report = map_attack_surface(agent)
        reports[agent.name] = report
        print(json.dumps(report, indent=2, ensure_ascii=False))

    print()
    _verify_reports(reports["anatomy_demo_agent"], reports["finance_admin_agent"])
