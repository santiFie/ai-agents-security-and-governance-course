#!/usr/bin/env python3
"""
Lab 3.2 — Workload Identity Simulada: Zero Trust sin SPIRE.

Un mini-SVID issuer (simula SPIRE Server) que emite JWTs RS256 de corta
duracion como identidad de workload, mas la clase AgentWorkloadIdentity que
un agente usaria para "atestiguar" su identidad y renovar su SVID.

Sin cambios de modelo: este lab NO usa google.adk ni ningun LLM en absoluto
-es JWT/criptografia pura via python-jose, y no depende de ningun backend de
modelo (Ollama/Gemini). El driver ejecuta los 4 ejercicios propuestos
(verificacion, expiracion, revocacion, integracion con la politica de OPA
del Lab 3.1) de punta a punta.

Requiere: python-jose[cryptography], cryptography. Instalados en un venv
local (venv_jwt/) porque el Python del sistema es 3.14 "externally managed"
y python-jose no se puede instalar con pip a nivel de sistema sin
--break-system-packages:
    python3 -m venv venv_jwt
    ./venv_jwt/bin/pip install "python-jose[cryptography]>=3.3.0" "cryptography>=41.0.0"

Correr:
    ./venv_jwt/bin/python3 workload_identity_lab32.py
"""
import datetime
import time
import uuid

from jose import jwt, JWTError
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.backends import default_backend


# ── Mini SVID Issuer (simula SPIRE Server) ──────────────────────────────────
class MiniSVIDIssuer:
    def __init__(self):
        # Generar par de claves RSA (en produccion: HSM o KMS)
        self.private_key = rsa.generate_private_key(
            public_exponent=65537, key_size=2048, backend=default_backend()
        )
        self.public_key = self.private_key.public_key()

    def issue_svid(self, spiffe_id: str, ttl_seconds: int = 3600) -> str:
        """
        Emite un SVID JWT de corta duracion para un workload.
        En produccion: SPIRE verifica attestation antes de emitir.
        """
        now = datetime.datetime.utcnow()
        payload = {
            "sub": spiffe_id,
            "iss": "spire-server.seminario.unlp.edu.ar",
            "iat": now,
            "exp": now + datetime.timedelta(seconds=ttl_seconds),
            "jti": str(uuid.uuid4()),  # JWT ID unico -- permite revocacion
            "spiffe_id": spiffe_id,
        }
        token = jwt.encode(payload, self.private_key, algorithm="RS256")
        print(f"[SVID emitido] sub={spiffe_id} ttl={ttl_seconds}s jti={payload['jti'][:8]}...")
        return token

    def verify_svid(self, token: str, revocation_list: set = None) -> dict:
        """Verifica y decodifica un SVID JWT."""
        try:
            claims = jwt.decode(
                token, self.public_key, algorithms=["RS256"],
                options={"verify_aud": False},
            )
            if revocation_list and claims.get("jti") in revocation_list:
                raise ValueError(f"SVID {claims['jti']} ha sido revocado")
            return claims
        except JWTError as e:
            raise ValueError(f"SVID invalido o expirado: {e}")


# ── Workload Identity para agente ───────────────────────────────────────────
class AgentWorkloadIdentity:
    def __init__(self, issuer: MiniSVIDIssuer, spiffe_id: str):
        self.issuer = issuer
        self.spiffe_id = spiffe_id
        self._current_svid = None
        self._svid_expires_at = None

    def get_svid(self) -> str:
        """Obtiene SVID actual, renovando si esta por expirar."""
        now = datetime.datetime.utcnow()
        if (
            self._current_svid is None
            or self._svid_expires_at - now < datetime.timedelta(minutes=5)
        ):
            # Renovar SVID (en produccion: via Workload API)
            self._current_svid = self.issuer.issue_svid(self.spiffe_id, ttl_seconds=3600)
            self._svid_expires_at = now + datetime.timedelta(seconds=3600)
            print(f"[SVID] Nuevo SVID emitido para {self.spiffe_id}")
        return self._current_svid


# ── Driver: ejercicios 1-4 ──────────────────────────────────────────────────
def ejercicio_1_verificar_svid_correcto(issuer: MiniSVIDIssuer) -> None:
    print("\n--- Ejercicio 1: verificar el SVID correctamente ---")
    svid = issuer.issue_svid("spiffe://seminario.unlp.edu.ar/agents/lab-agent")
    claims = issuer.verify_svid(svid)
    assert claims["spiffe_id"] == "spiffe://seminario.unlp.edu.ar/agents/lab-agent"
    print(f"OK: SVID verificado correctamente. Claims: sub={claims['sub']}, "
          f"iss={claims['iss']}, jti={claims['jti'][:8]}...")


def ejercicio_2_ttl_corto_expira(issuer: MiniSVIDIssuer) -> None:
    print("\n--- Ejercicio 2: SVID con TTL de 5s, esperar 6s, verificar que falla ---")
    svid = issuer.issue_svid("spiffe://seminario.unlp.edu.ar/agents/short-lived", ttl_seconds=5)
    print("Esperando 6 segundos para que expire...")
    time.sleep(6)
    try:
        issuer.verify_svid(svid)
        raise AssertionError("El SVID expirado NO deberia haberse verificado con exito")
    except ValueError as e:
        print(f"OK: SVID expirado correctamente rechazado -> {e}")


def ejercicio_3_revocacion(issuer: MiniSVIDIssuer) -> None:
    print("\n--- Ejercicio 3: revocation_list, agregar el JTI del SVID, verificar que falla ---")
    svid = issuer.issue_svid("spiffe://seminario.unlp.edu.ar/agents/revoked-agent")
    claims_unverified = jwt.get_unverified_claims(svid)
    jti = claims_unverified["jti"]

    revocation_list = set()
    # Primero: sin revocar, debe pasar
    claims = issuer.verify_svid(svid, revocation_list=revocation_list)
    print(f"Antes de revocar: SVID valido -> jti={claims['jti'][:8]}...")

    # Ahora: revocar y verificar que falla aunque no haya expirado
    revocation_list.add(jti)
    try:
        issuer.verify_svid(svid, revocation_list=revocation_list)
        raise AssertionError("El SVID revocado NO deberia haberse verificado con exito")
    except ValueError as e:
        print(f"OK: SVID revocado correctamente rechazado (aunque no expiro) -> {e}")


def ejercicio_4_integracion_con_opa(identity: AgentWorkloadIdentity) -> dict:
    """Integra identity.get_svid() con el policy check de OPA del Lab 3.1.

    No requiere que OPA este corriendo para este lab: arma el `policy_input`
    que Lab 3.1 (check_opa) enviaria a OPA, usando el spiffe_id extraido del
    SVID real (no hardcodeado), y lo devuelve. Si se quiere completar la
    integracion real, ese dict es exactamente el payload que
    `check_opa()`/`load_opa_policy()` de ch03-lab1-ollama/opa_authz_lab31.py
    esperan en `POST /v1/data/agent_authz/decision`.
    """
    print("\n--- Ejercicio 4: integrar identity.get_svid() con el policy check de OPA (Lab 3.1) ---")
    svid = identity.get_svid()
    claims = identity.issuer.verify_svid(svid)
    policy_input = {
        "input": {
            "subject": {"spiffe_id": claims["spiffe_id"]},
            "resource": {"api_path": "/api/v1/sales_data"},
            "action": {"http_method": "GET"},
        }
    }
    print(f"OK: policy_input armado a partir del SVID real (verificado, no confiado a ciegas):")
    print(f"    {policy_input}")
    print("    (para probarlo contra OPA de verdad: POST este payload a "
          "http://localhost:8181/v1/data/agent_authz/decision, con OPA "
          "corriendo y la politica de ch03-lab1-ollama/opa_authz_lab31.py cargada)")
    return policy_input


def main() -> None:
    print("=" * 70)
    print("Lab 3.2: Workload Identity Simulada (JWT, sin SPIRE, sin LLM)")
    print("=" * 70)

    issuer = MiniSVIDIssuer()
    identity = AgentWorkloadIdentity(
        issuer, "spiffe://seminario.unlp.edu.ar/agents/lab-agent"
    )

    # Demo minima del flujo (get_svid renovando lazy)
    svid = identity.get_svid()
    print(f"\nSVID inicial obtenido (primeros 40 chars): {svid[:40]}...")

    ejercicio_1_verificar_svid_correcto(issuer)
    ejercicio_2_ttl_corto_expira(issuer)
    ejercicio_3_revocacion(issuer)
    policy_input = ejercicio_4_integracion_con_opa(identity)

    assert "spiffe_id" in policy_input["input"]["subject"]
    print("\n" + "=" * 70)
    print("OK: Lab 3.2 verificado de punta a punta (Ejercicios 1-4).")
    print("=" * 70)


if __name__ == "__main__":
    main()
