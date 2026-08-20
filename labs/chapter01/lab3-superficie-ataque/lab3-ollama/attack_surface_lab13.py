#!/usr/bin/env python3
"""
Lab Propuesto 1.3: Mapeo de Superficie de Ataque — Herramienta de Auditoria.

Adaptado del "Codigo esqueleto" del Lab Propuesto 1.3 (ch01-labs.md, lineas
239-280). A diferencia de los Labs 1.1 y 1.2, este NO invoca ningun LLM: es
introspeccion estatica via `inspect` sobre `agent.tools` / `agent.model`. Por
eso corre de punta a punta sin Ollama, sin GPU y sin red -es la unica pieza
de este capitulo que no necesita el swap Gemini -> modelo local.

Cambio respecto al original: se corrigio un bug latente en
`map_attack_surface()`. El libro asume que `agent.model` siempre es un
string (p.ej. "gemini-2.0-flash") y hace `"flash" in model_name` -eso rompe
con TypeError si `agent.model` es un objeto LiteLlm (que es exactamente lo
que usan los agentes de este seminario adaptados a Ollama, ver Lab 1.1/1.2).
Se agrego `_extract_model_name()` para soportar ambos casos y se instancian
los agentes de demostracion con LiteLlm(model="ollama_chat/qwen3.5:9b", ...)
en vez de model="gemini-2.0-flash", consistente con el resto del capitulo -
aunque para ESTE lab en particular el nombre del modelo es un dato mas a
inspeccionar, nunca se invoca.

Requiere: google-adk, litellm (solo para construir LiteLlm(...), no se
conecta a Ollama en ningun momento de este script).

Ejecucion (no toca Ollama, no requiere flag especial):
    python3 attack_surface_lab13.py
"""
import inspect
import json

from google.adk.agents import Agent
from google.adk.models.lite_llm import LiteLlm


def _extract_model_name(agent: Agent) -> str:
    """Devuelve un string identificando el modelo del agente, sea un string
    plano (p.ej. 'gemini-2.0-flash') o un wrapper como LiteLlm (usado en
    este seminario para correr contra Ollama local). El original del libro
    asumia siempre un string y rompia con TypeError al hacer `"flash" in
    model_name` sobre un objeto no iterable de esa forma."""
    m = getattr(agent, "model", "unknown")
    if isinstance(m, str):
        return m
    return getattr(m, "model", str(m))


# Peso por nivel de riesgo para el scoring de tools (ver "Hallazgo 2" en
# attack_surface_lab13.md): la version original del libro contaba tools con un peso fijo de
# 3 cada una, sin mirar el campo "risk" que map_attack_surface() ya
# calculaba -por eso un agente de solo lectura/escritura de archivos
# (MEDIUM/MEDIUM) y uno que emite creditos y borra cuentas (HIGH/HIGH)
# terminaban con el mismo attack_surface_score pese a tener perfiles de
# riesgo opuestos. Verificado en vivo (2026-08-08): con este ponderado,
# benign_agent -> 7.5, high_risk_agent -> 13.5 (antes, los dos daban 9.5).
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
#    Ollama). Reproducen el perfil de riesgo de los agentes de Lab 1.1 (bajo
#    riesgo: filesystem local) y Lab 1.2 (alto riesgo: emision de creditos +
#    una tool administrativa extra para ilustrar el filtro por keyword). ───
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


_LOCAL_MODEL_KWARGS = dict(
    model="ollama_chat/qwen3.5:9b",
    num_ctx=8192,
    temperature=0.2,
    reasoning_effort="none",
)

benign_agent = Agent(
    name="anatomy_demo_agent",
    description="Agente de bajo riesgo: solo lee/escribe archivos locales (perfil Lab 1.1).",
    model=LiteLlm(**_LOCAL_MODEL_KWARGS),
    instruction="Completa las tareas solicitadas usando las herramientas disponibles.",
    tools=[read_file, write_file],
)

high_risk_agent = Agent(
    name="finance_admin_agent",
    description="Agente de alto riesgo: emite creditos y administra cuentas (perfil Lab 1.2).",
    model=LiteLlm(**_LOCAL_MODEL_KWARGS),
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
    # FORMULA PONDERADA POR RIESGO (2026-08-08): la version original de
    # attack_surface_score() contaba tools con un peso fijo de 3 cada una,
    # sin mirar el campo "risk" -eso hacia que un agente de solo
    # lectura/escritura de archivos (MEDIUM/MEDIUM) y uno que emite
    # creditos y borra cuentas (HIGH/HIGH) terminaran con el MISMO score
    # (9.5 los dos), pese a tener perfiles de riesgo opuestos. Con el
    # ponderado actual (_TOOL_RISK_WEIGHT: HIGH=5, MEDIUM=2), el score
    # ahora SI distingue: benign_agent -> 7.5, high_risk_agent -> 13.5. Se
    # asserta la desigualdad, y que el de mayor riesgo tenga mayor score,
    # explicitamente -para que quede como hallazgo verificado que la
    # ponderacion funciona, no solo que "algun numero salio".
    assert high_risk_report["attack_surface_score"] > benign_report["attack_surface_score"], (
        "con la formula ponderada por risk, high_risk_agent deberia tener "
        "un attack_surface_score mayor que benign_agent -si dan igual o al "
        "reves, revisar _TOOL_RISK_WEIGHT en map_attack_surface()"
    )
    assert any(t["risk"] == "HIGH" for t in high_risk_report["tool_interfaces"]) and all(
        t["risk"] == "MEDIUM" for t in benign_report["tool_interfaces"]
    ), "precondicion del hallazgo: los perfiles de riesgo deben ser realmente distintos"
    for report in (benign_report, high_risk_report):
        assert report["input_channels"][0]["model"] == "ollama_chat/qwen3.5:9b", (
            "_extract_model_name deberia leer el string interno del LiteLlm, no el objeto"
        )
        assert len(report["input_channels"]) == 1, (
            "qwen3.5:9b via ollama_chat no deberia agregar el canal 'image' "
            "(el nombre del modelo no contiene 'flash' ni 'pro')"
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
