# Prerequisites — Telecom Automation Project

This document lists the background knowledge and tooling each team member must have (or acquire) before contributing to the project.

---

## 1. 4G / 5G Architecture Fundamentals

### 1.1 System Overview
- Understand the split between **Radio Access Network (RAN)** and **Core Network (CN)**
- Know the difference between **4G LTE** (EPC + eNB) and **5G NR** (5GC + gNB) architectures
- Understand **Non-Standalone (NSA)** vs **Standalone (SA)** 5G deployment modes

### 1.2 RAN Components
| Component | Full Name | Key Function |
|---|---|---|
| UE | User Equipment | End device (phone, IoT sensor, …) |
| RU | Radio Unit | RF front-end, antenna, lower PHY |
| DU | Distributed Unit | Real-time upper PHY + MAC + RLC |
| CU | Centralized Unit | Non-real-time PDCP + RRC + SDAP |
| CU-CP | CU Control Plane | RRC, PDCP-C |
| CU-UP | CU User Plane | PDCP-U, SDAP |

Key interfaces: **F1** (CU↔DU), **E1** (CU-CP↔CU-UP), **Uu** (RU↔UE), **fronthaul** (RU↔DU), **midhaul** (DU↔CU)

### 1.3 5G Core (5GC) Components
| Component | Function |
|---|---|
| AMF | Access & Mobility Management — NAS, registration, handover |
| SMF | Session Management — PDU session, IP allocation |
| UPF | User Plane Function — packet forwarding, QoS enforcement |
| PCF | Policy Control — charging, QoS policy |
| UDM | Unified Data Management — subscriber data |
| AUSF | Authentication Server |
| NRF | Network Repository Function — service discovery |

### 1.4 O-RAN Architecture
- Understand the **O-RAN Alliance** functional split (Split 7.2x is most common)
- Know the roles of: **O-RU**, **O-DU**, **O-CU**, **Near-RT RIC**, **Non-RT RIC**, **SMO**
- Understand **xApp** and **rApp** as applications on the RIC
- Understand the **O1**, **O2**, **A1**, **E2** interfaces

### 1.5 Network Slicing
- What is a **network slice** (eMBB, URLLC, mMTC)?
- How slices map to **S-NSSAIs** and **NSSTs**
- Basic slice lifecycle: creation, modification, deletion via SMO

### 1.6 Radio Planning Concepts
- **PCI (Physical Cell ID)**: range 0–1007, must be collision-free and confusion-free within a cluster
- **Coverage vs capacity** trade-off
- **Fronthaul latency budget**: typically < 100 µs one-way for CPRI/eCPRI
- **Timing synchronization**: GPS PPS, IEEE 1588 PTP, SyncE — why it matters for TDD

---

## 2. Infrastructure & Tooling

### 2.1 Kubernetes (K8s)
- Pods, Deployments, Services, ConfigMaps, Secrets
- Helm charts: writing and installing
- `kubectl` basics: apply, get, describe, logs, exec
- Namespaces and RBAC basics

### 2.2 Containerization
- Docker: build, run, push images
- Multi-stage Dockerfiles
- Container networking basics (bridge, host, overlay)

### 2.3 Python (3.11+)
- `asyncio` and `httpx` for async HTTP calls
- `pydantic` v2 for data validation and models
- `pytest` for testing (fixtures, parametrize, mocking)
- Virtual environments (`venv` or `conda`)

### 2.4 Anthropic SDK / Claude API
- Tool-calling (function calling) with `anthropic` Python SDK
- Prompt caching (`cache_control` headers)
- Streaming responses
- Reference: https://docs.anthropic.com

### 2.5 Databases
- SQL basics (PostgreSQL preferred): CREATE TABLE, INSERT, SELECT, JOIN
- Or NoSQL if chosen: document structure, indexing
- ORM basics (`SQLAlchemy` or `SQLModel`)

---

## 3. Recommended Learning Resources

### 4G/5G
- *5G NR: The Next Generation Wireless Access Technology* — Dahlman et al. (Chapter 1–4 minimum)
- 3GPP TS 38.401 — NG-RAN Architecture (free download from 3gpp.org)
- O-RAN Alliance white papers: o-ran.org/specifications
- YouTube: "5G Architecture Explained" series by various telecom educators

### O-RAN / OpenRAN
- OpenAirInterface (OAI) documentation: openairinterface.org
- srsRAN documentation: docs.srsran.com
- ONF SD-RAN: opennetworking.org/sd-ran

### Kubernetes / Helm
- Official K8s docs: kubernetes.io/docs
- Helm docs: helm.sh/docs
- *Kubernetes in Action* — Marko Lukša (Chapters 1–6)

### LLM / Agent Development
- Anthropic tool use guide: docs.anthropic.com/en/docs/tool-use
- *Building LLM-Powered Applications* patterns (ReAct, tool-calling, multi-agent)

---

## 4. Environment Setup Checklist

Before your first commit, confirm you have:

- [ ] Python 3.11+ installed and a project virtualenv active
- [ ] `pip install anthropic pydantic httpx pytest sqlalchemy`
- [ ] `kubectl` installed and configured (or `kind`/`minikube` for local testing)
- [ ] `helm` installed (v3+)
- [ ] Docker Desktop (or Podman) running
- [ ] Access to `ANTHROPIC_API_KEY` (get from Anthropic console)
- [ ] Git configured with your IISc email
- [ ] Cloned the project repository and run `pytest tests/unit/` with no errors

---

## 5. Key Questions to Clarify with the Team Lead

Before starting your module, answer these questions (see `spec.md` Open Questions for full list):

1. Which SMO implementation are we targeting (mock / OAI / ONF)?
2. Is there a shared K8s cluster, or do you run `kind` locally?
3. What is the chosen LLM model for the orchestrator?
4. Which geographic area / dataset is used for the demo?
5. What is your assigned agent module (Orchestrator / Core DB / Deployment / KPI)?
