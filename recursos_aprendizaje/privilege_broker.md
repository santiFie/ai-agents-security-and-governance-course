# Privilege Broker: elevación de privilegios Just-in-Time para agentes

> **Por qué este documento.** El Tema 3 (Identidad y Autenticación de
> Agentes de IA) cubre SPIFFE/SPIRE como mecanismo de identidad de workload
> (Lab 3.2) y OPA/Rego como Policy Decision Point para autorización
> (Lab 3.1). El Privilege Broker es el componente que conecta ambos
> conceptos en un patrón de arquitectura concreto: usa la identidad SPIFFE
> como credencial base para decidir, vía un PDP tipo OPA, si un agente
> puede recibir una credencial elevada y temporal. Este documento junta ese
> patrón completo —qué es, cómo se integra con SPIFFE/SPIRE, un ejemplo de
> política Rego, y cómo se despliega en entornos heterogéneos— en un solo
> lugar.

## Qué es un Privilege Broker

En una arquitectura de seguridad para agentes, un **Privilege Broker** es
un componente de software intermedio (middleware / servicio de
gobernanza) que actúa como un **PEP/PDP delegado** (Policy Enforcement
Point / Policy Decision Point) específicamente para la **elevación de
privilegios**.

Su función principal es:

1. Validar la identidad soberana del agente.
2. Verificar las políticas de autorización contextuales.
3. Emitir credenciales temporales de corto alcance (*ephemeral
   credentials*).

Todo esto evitando el aprovisionamiento permanente de permisos
(*standing privileges*).

## Just-in-Time (JIT) Access: privilegio bajo demanda

Un agente orquestador puede necesitar acceso elevado temporal para una
tarea puntual. Otorgarlo de forma permanente viola el principio de mínimo
privilegio — la solución es el **JIT Access**: el privilegio se concede
solo cuando hace falta, por el tiempo mínimo necesario, y se revoca solo
al vencer.

### Flujo JIT, paso a paso

1. El agente determina que necesita acceso elevado.
2. Llama al Privilege Broker autenticándose con su **SVID base**,
   especificando el permiso y la duración solicitados.
3. El Broker (con su propio PDP) evalúa si ese agente puede solicitar ese
   permiso en ese contexto.
4. Si aprueba: emite una credencial de corta vida y alcance estrecho
   (Verifiable Credential, token OAuth, o una política temporal).
5. El agente usa esa credencial para acceder al servicio objetivo.
6. Al expirar, el acceso se revoca automáticamente.

## Funciones y responsabilidades técnicas

- **Validación de Identidad de Agente**: autentica la identidad base del
  agente mediante mecanismos criptográficos fuertemente atados a su ciclo
  de vida (como un SVID en el estándar SPIFFE/SPIRE).
- **Evaluación de Políticas Contextuales (PDP)**: determina si la
  solicitud procede según el estado actual, el tipo de tarea declarada, el
  riesgo asociado y los atributos de la sesión (ABAC/RBAC dinámico).
- **Emisión de Credenciales Efímeras**: intercambia la credencial base por
  un secreto de alcance restringido (por ejemplo, un token OAuth con
  *scopes* acotados, una Verifiable Credential firmada, o una política IAM
  de corta duración / STS en la nube).
- **Gobernanza y Trazabilidad**: registra el evento de elevación (quién,
  qué, cuándo y por qué) para auditoría de seguridad, y garantiza la
  expiración/revocación automática del acceso sin intervención manual.

### Arquitectura simplificada de integración

```
[ Agente (SVID Base) ] --(1. Solicita JIT Access)--> [ Privilege Broker (PDP) ]
                                                            |
                                                   (2. Evalúa Políticas)
                                                            |
[ Servicio Objetivo ] <--(3. Consume con Token Efímero)-- [ Agente (SVID Temporal) ]
```

## Integración de SPIFFE/SPIRE en la arquitectura

En una arquitectura agéntica de JIT Access, SPIFFE/SPIRE provee la **raíz
de confianza** (Root of Trust) y la identidad criptográfica
*workload-to-workload*.

El SPIFFE ID codificado en un SVID (SPIFFE Verifiable Identity Document,
habitualmente un certificado X.509 v3 o un JWT) funciona como el documento
de identidad base con el que el Agente se presenta ante el Privilege
Broker.

### Componentes en el flujo de integración

- **SPIRE Server**: autoridad de certificación (CA) de la malla de
  identidad. Firma y valida la expedición de SVIDs basándose en reglas de
  atestación.
- **SPIRE Agent**: daemon local en el nodo donde corre el agente o el
  contenedor. Realiza la atestación de la carga de trabajo (*workload
  attestation*) inspeccionando metadatos del entorno (PID, SHA256 del
  binario, namespace de Kubernetes, cgroup, etc.).
- **Workload API**: socket Unix local expuesto por el SPIRE Agent, a
  través del cual los componentes obtienen y renuevan sus SVIDs de forma
  transparente.
- **Agente Orquestador (Workload)**: consume el SVID base sin manejar
  secretos estáticos (keys, passphrases) hardcodeados en código o
  variables de entorno.
- **Privilege Broker**: actúa como un SPIFFE Workload / Verifier que
  valida criptográficamente el SVID contra el Trust Bundle del dominio de
  SPIFFE.

### Flujo técnico

```
 [ SPIRE Agent ] <---(1. Attestation / Socket)---> [ Agente Orquestador ]
        |                                                 |
(Emite SVID Base)                                         |
        v                                                 | (2. Mutual TLS / SVID)
 [ Workload API ]                                         v
                                                [ Privilege Broker ]
                                                          |
                                                 (3. Valida vs Bundle)
                                                          v
                                                [ SPIRE Trust Bundle ]
```

**1. Atestación e inyección del SVID base**

El Agente Orquestador se inicia en el entorno de ejecución. Para obtener
su identidad base:

- Realiza una llamada al Workload API del SPIRE Agent local.
- El SPIRE Agent valida que el proceso cumple con la política de
  atestación registrada en el SPIRE Server (por ejemplo,
  `spiffe://example.org/ns/agents/sa/orchestrator`).
- SPIRE entrega al agente su SVID base (X.509 o JWT) y el Trust Bundle
  público de la organización.

**2. Autenticación mTLS / Token ante el Privilege Broker**

Cuando el Agente determina que requiere permisos elevados para una tarea
puntual, inicia una conexión segura con el Privilege Broker:

- **Opción A — mTLS (recomendada para X.509 SVID)**: handshake TLS mutuo,
  donde el Agente presenta su certificado X.509-SVID como credencial de
  cliente.
- **Opción B — JWT SVID**: en un encabezado HTTP
  (`Authorization: Bearer <JWT-SVID>`), firma la solicitud incluyendo el
  *audience* específico del Privilege Broker.

**3. Verificación e inspección por parte del Broker**

El Privilege Broker recibe la solicitud de elevación de privilegios y
realiza la verificación de nivel de plataforma:

- **Validación criptográfica**: contrasta la firma del SVID contra el
  SPIFFE Trust Bundle (público) proporcionado por el SPIRE Server.
- **Extracción del SPIFFE ID**: extrae el campo SAN (Subject Alternative
  Name) del certificado o el `sub` del JWT, obteniendo la identidad fuerte
  (por ejemplo, `spiffe://prod.domain/agent/orchestrator-node-01`).
- **Verificación de expiración/revocación**: valida que el SVID base esté
  vigente (los SVIDs, por especificación, son de corta vida, típicamente
  renovados cada pocas horas).

**4. Delegación al PDP (Policy Decision Point)**

Una vez validada la identidad soberana del Agente mediante SPIFFE/SPIRE,
el Broker utiliza el SPIFFE ID extraído como el atributo principal del
*subject* para evaluar las reglas de negocio en su PDP interno (por
ejemplo, integrando Open Policy Agent):

```json
{
  "input": {
    "subject": "spiffe://prod.domain/agent/orchestrator-node-01",
    "requested_permission": "s3:GetObject",
    "resource": "arn:aws:s3:::audit-logs-sensitive/*",
    "context": {
      "task_id": "task-88321",
      "duration": "300s"
    }
  }
}
```

**5. Emisión del token de acceso efímero**

Si la política aprueba la solicitud contextual, el Privilege Broker asume
una identidad de servicio (por ejemplo, invocando `sts:AssumeRole` en AWS,
o emitiendo un JWT firmado de corta vida / Verifiable Credential) y
retorna la credencial efímera al Agente, completando el ciclo JIT.

### Beneficios clave del patrón SPIFFE/SPIRE + Privilege Broker

- **Zero Secret Persistence**: el Agente no almacena API keys ni tokens de
  administración de larga duración en ningún archivo de configuración o
  secreto del orquestador.
- **Identidad basada en atributos de ejecución (attestation)**: si el
  binario o el contenedor del Agente es alterado por un atacante, la
  atestación de SPIRE falla y el Agente pierde la capacidad de solicitar
  SVIDs base — y, por ende, pierde el acceso al Privilege Broker.
- **Segmentación estricta**: el Privilege Broker no necesita conocer los
  detalles de infraestructura donde corre el Agente; únicamente confía en
  la cadena de certificación de SPIFFE.

## Ejemplo de política Rego para OPA

Esta política Rego (diseñada para OPA v1.0+) implementa la lógica de
decisión del PDP dentro del Privilege Broker. Evalúa la identidad
criptográfica derivada del SVID de SPIFFE, valida el alcance solicitado
(`permission` y `resource`), comprueba que la duración no exceda los
límites permitidos por rol, y exige la presencia de un contexto operativo
válido (ID de tarea/ticket).

### Código de la política (`policy.rego`)

```rego
package privilege_broker.jit

import rego.v1

# Por defecto, la solicitud es denegada (Fail-Closed)
default allow := false

# El payload devuelto al Broker incluye el resultado, la razón y los límites aplicados
response := {
    "allow": allow,
    "reasons": denial_reasons,
    "granted_ttl": max_allowed_duration,
}

# -----------------------------------------------------------------------------
# Regla Principal de Permisión
# -----------------------------------------------------------------------------
allow if {
    count(denial_reasons) == 0
}

# -----------------------------------------------------------------------------
# Validaciones / Razones de Denegación
# -----------------------------------------------------------------------------
denial_reasons contains "INVALID_SPIFFE_ID_FORMAT" if {
    not valid_spiffe_prefix
}

denial_reasons contains "UNAUTHORIZED_AGENT_ROLE" if {
    valid_spiffe_prefix
    not agent_role
}

denial_reasons contains "PERMISSION_NOT_ALLOWED_FOR_ROLE" if {
    agent_role
    not permission_allowed_for_role
}

denial_reasons contains "EXCEEDED_MAX_DURATION" if {
    input.context.duration_seconds > max_allowed_duration
}

denial_reasons contains "MISSING_TASK_CONTEXT" if {
    not valid_task_context
}

# -----------------------------------------------------------------------------
# Lógica Auxiliar de Evaluación
# -----------------------------------------------------------------------------

# Convención de SPIFFE ID: spiffe://<domain>/ns/<namespace>/sa/<agent_role>
valid_spiffe_prefix if {
    startswith(input.spiffe_id, "spiffe://prod.domain/ns/agents/sa/")
}

# Extrae el rol del agente a partir del SPIFFE ID
agent_role := role if {
    valid_spiffe_prefix
    parts := split(input.spiffe_id, "/")
    role := parts[count(parts) - 1]
}

# Mapeo de Matriz de Accesos (RBAC/ABAC) según el rol del Agente
role_permissions := {
    "data-orchestrator": {
        "allowed_permissions": ["s3:GetObject", "s3:PutObject", "db:ReadSchema"],
        "allowed_resources": ["arn:aws:s3:::data-lake-raw/*", "arn:aws:s3:::data-lake-processed/*"],
        "max_ttl_seconds": 900 # 15 minutos
    },
    "remediation-agent": {
        "allowed_permissions": ["ec2:RebootInstances", "k8s:pods/restart"],
        "allowed_resources": ["arn:aws:ec2:*:*:instance/*", "k8s:cluster-prod:*"],
        "max_ttl_seconds": 300 # 5 minutos
    }
}

# Verifica si el permiso y el recurso están explícitamente autorizados para el rol
permission_allowed_for_role if {
    role_config := role_permissions[agent_role]
    input.requested_permission == role_config.allowed_permissions[_]
    glob.match(role_config.allowed_resources[_], [":"], input.requested_resource)
}

# Determina la duración máxima permitida según la política del rol
max_allowed_duration := duration if {
    duration := role_permissions[agent_role].max_ttl_seconds
} else := 0

# Valida que la solicitud incluya una justificación trazable (Tarea / Issue Ticket)
valid_task_context if {
    input.context.task_id != ""
    regex.match(`^TASK-[0-9]{4,6}$`, input.context.task_id)
}
```

### Estructura del input de entrada (`input.json`)

Representa los datos extraídos por el Broker tras autenticar al agente
mediante mTLS/JWT-SVID:

```json
{
  "spiffe_id": "spiffe://prod.domain/ns/agents/sa/data-orchestrator",
  "requested_permission": "s3:GetObject",
  "requested_resource": "arn:aws:s3:::data-lake-raw/ingestion-2026/file.parquet",
  "context": {
    "duration_seconds": 300,
    "task_id": "TASK-88421"
  }
}
```

### Ejemplo del output de evaluación de OPA (`response`)

**Solicitud aprobada**:

```json
{
  "allow": true,
  "granted_ttl": 900,
  "reasons": []
}
```

**Solicitud denegada** (por ejemplo, si se excede la duración o el formato
del ticket es inválido):

```json
{
  "allow": false,
  "granted_ttl": 900,
  "reasons": [
    "EXCEEDED_MAX_DURATION",
    "MISSING_TASK_CONTEXT"
  ]
}
```

## Estrategias para entornos heterogéneos

En entornos híbridos y heterogéneos (donde coexisten clústeres de
Kubernetes, máquinas virtuales en múltiples nubes públicas, servidores
*bare-metal* e incluso instancias *edge*), desplegar la arquitectura de
identidad SPIFFE/SPIRE exige adaptar las estrategias de **atestación de
nodos** (Node Attestation) y **atestación de cargas de trabajo**
(Workload Attestation). A continuación se detallan las estrategias clave
para garantizar que el Privilege Broker confíe en los SVIDs base
generados en cualquier infraestructura.

### 1. Estrategia de atestación de nodos (Node Attestation)

El SPIRE Server debe validar la identidad del host/nodo donde se ejecuta
el SPIRE Agent antes de permitirle emitir identidades a los agentes o
procesos locales. Se deben combinar diferentes *Node Attestors* según el
entorno:

- **Entornos cloud públicos (AWS, GCP, Azure)**
  - *Mecanismo*: Cloud Instance Identity Documents (por ejemplo, AWS IID,
    GCP JWT, Azure MSI).
  - *Funcionamiento*: el SPIRE Agent consulta el API de metadatos de la
    instancia (IMDS) al iniciar, obtiene un documento firmado
    criptográficamente por el proveedor cloud, y se lo presenta al SPIRE
    Server.

- **Clústeres de Kubernetes (EKS, GKE, on-premise)**
  - *Mecanismo*: `k8s_psat` (Projected Service Account Token).
  - *Funcionamiento*: el SPIRE Agent utiliza tokens de ServiceAccount
    proyectados y de vida corta, firmados por el API Server de
    Kubernetes, para atestar el nodo del clúster.

- **Servidores on-premise / bare-metal**
  - *Mecanismo*: TPM 2.0 (Trusted Platform Module) o X.509 PoP (Proof of
    Possession).
  - *Funcionamiento*: uso de claves privadas grabadas en el chip de
    seguridad del hardware (*endorsement keys*) o certificados X.509
    pre-aprovisionados durante el *bootstrapping* del servidor.

### 2. Estrategia de atestación de cargas de trabajo (Workload Attestation)

Una vez atestado el nodo, el SPIRE Agent atesta el proceso local que
solicita un SVID a través del Unix Domain Socket (Workload API).

- **En Kubernetes**
  - *Attestors*: `k8s:ns` (namespace), `k8s:sa` (service account),
    `k8s:pod-label`, `k8s:container-image`.
  - *Reglas*: se mapea la identidad SPIFFE directamente con la metadata
    declarativa del Pod.

- **En VMs / Linux genérico**
  - *Attestors*: `unix:uid`, `unix:gid`, `unix:user`, `unix:path` (ruta
    del binario), `unix:sha256` (hash criptográfico del binario).
  - *Reglas*: garantiza que solo el ejecutable binario exacto con la
    firma/hash correspondiente, ejecutado bajo un usuario del sistema
    específico, pueda recibir el SVID.

### 3. Matriz de mapeo de identidad (diseño del SPIFFE ID)

Para mantener una gobernanza coherente en el Privilege Broker y las
políticas de OPA, es fundamental estructurar un esquema de jerarquía
unificada para los SPIFFE IDs (Trust Domain):

| Entorno | Ejemplo de SPIFFE ID de carga de trabajo | Selectores combinados |
|---|---|---|
| Kubernetes (Prod) | `spiffe://domain.com/ns/prod/sa/agent-orchestrator` | `k8s:ns:prod`, `k8s:sa:agent-orchestrator` |
| AWS EC2 (VM) | `spiffe://domain.com/region/us-east-1/app/agent-worker` | `aws:iam_role:...`, `unix:path:/usr/bin/agent` |
| Bare-Metal On-Prem | `spiffe://domain.com/datacenter/dc1/app/agent-worker` | `tpm:has_tpm:true`, `unix:sha256:e3b0c442...` |

### 4. Arquitectura de malla de identidad: Federated Trust Domains

En escenarios multicloud, u organizaciones con aislamiento estricto por
regulación, la mejor práctica es evitar un único punto de fallo mediante
la **federación SPIFFE**:

```
[ Domain AWS (Trust Domain 1) ] <--- Federation (JWKS / Bundle Swap) ---> [ Domain On-Prem (Trust Domain 2) ]
          |                                                                           |
   (SVID: spiffe://aws.domain/...)                                            (SVID: spiffe://onprem.domain/...)
          \                                                                           /
           +---------------------> [ Privilege Broker Central ] <--------------------+
                                 (Valida ambos Trust Bundles)
```

- **Servidores SPIRE independientes**: cada región, nube o datacenter
  opera su propio par redundante de SPIRE Server / Trust Domain
  (`spiffe://aws.domain` vs. `spiffe://onprem.domain`).
- **Intercambio de Trust Bundles**: los SPIRE Servers intercambian de
  forma automatizada sus claves públicas (vía *endpoints* HTTPS seguros o
  *endpoints* OIDC/JWKS).
- **Soporte en el Privilege Broker**: el Privilege Broker almacena
  múltiples Trust Bundles federados, permitiéndole validar las
  solicitudes JIT de un agente independientemente de si proviene de un
  nodo en AWS o de un servidor en un datacenter local.

### Resumen de recomendaciones de despliegue

- **Reducir el *blast radius***: configurar SVIDs con tiempos de
  expiración muy cortos (por ejemplo, 1 hora) e implementar renovación
  automática continua vía SPIRE Agent.
- **Principio de inmutabilidad**: en VMs, priorizar atestaciones basadas
  en el hash SHA256 del binario y firmas de código sobre atributos
  dinámicos como el `uid` de usuario.
- **Soporte *fall-back***: si un entorno no soporta atestación nativa por
  hardware/cloud (por ejemplo, entornos legacy), utilizar atestación en
  cascada mediante *Join Tokens* efímeros aprovisionados por un pipeline
  CI/CD verificado durante el despliegue.

## Para seguir pensando

1. La política Rego de este documento es *fail-closed* por defecto
   (`default allow := false`). ¿Qué tendría que fallar en el Privilege
   Broker para que ese diseño deje de proteger — es decir, en qué
   condición un fail-closed termina comportándose como fail-open?
2. El `max_allowed_duration` depende enteramente del rol extraído del
   SPIFFE ID (`agent_role`). Si un atacante pudiera registrar un agente
   con un SPIFFE ID que termine coincidiendo con un rol de mayor
   privilegio (por ejemplo, por un error de nomenclatura), ¿qué otra
   validación de este documento evitaría la escalación?
3. Compará este patrón con el Lab 3.1 (OPA) y el Lab 3.2 (Workload
   Identity) del curso: ¿qué parte del Privilege Broker ya está cubierta
   por esos dos labs combinados, y qué agrega específicamente el concepto
   de JIT Access que ninguno de los dos cubre por separado?
4. La federación de Trust Domains resuelve la confianza *entre*
   organizaciones/nubes. ¿Qué pasaría si uno de los Trust Bundles
   federados quedara comprometido — qué alcance tendría el daño, y cómo
   lo limitaría el diseño de dominios de confianza separados?
