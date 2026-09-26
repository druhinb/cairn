# Candidate profile — fit preferences

The ranker reads this file to score how well each posting fits. This example
describes a made-up candidate; replace every line with your own.

## Snapshot
- Alex Rivera — B.S. Computer Science, State University, GPA 3.90, expected Jun 2027.
- Applying for new-grad / early-career roles **starting mid-2027**, after I graduate.
  A role that starts before then is not one I can take, however good it looks.
- Also applying for **off-season internships** in the terms I am still a student for:
  Fall 2026, Winter 2027, Spring 2027. Summer is out. I have committed Summer 2026
  already, and Summer 2027 falls after graduation. Score these on the same
  role fit and company calibre as a full-time posting; an internship at a strong
  engineering company is a better lead than a full-time role at a weak one.
- Work authorization: **U.S. Citizen** (I need no sponsorship, so do not rank lower on that basis).

## Target roles (higher fit)
- Software Engineer (backend, distributed systems, infrastructure, platform)
- Quant / Quantitative Developer (low-latency, C++, systems)
- Research Engineer / ML Engineer / ML Infra
- Forward-Deployed Software Engineer (FDSE) / Solutions Engineer at strong eng companies

## Strengths to match on
- Distributed systems & low-latency backend (Northwind: 40K events/sec, sub-200ms p99).
- Systems / low-level: C, C++, Rust, operating systems, a from-scratch inference engine.
- ML/AI infra: PyTorch, vector search, retrieval pipelines, evaluation harnesses.
- Observability & cloud: AWS, Kubernetes, Postgres, OpenTelemetry/Prometheus/Grafana.

## Rank higher when a posting mentions
- Distributed systems, low latency, high throughput, performance, C++/Rust, systems programming,
  infra/platform, serverless/compute, quant/trading, LLMs/ML infra, new-grad rotational programs,
  or companies known for strong engineering culture.

## Rank lower / skip
- Frontend-only, IT/helpdesk, non-technical, hardware-only, roles requiring 3+ years experience,
  or on-site-only in cities I won't relocate to.

## Company tier (drives the `tier` score, 0-100)

Judge the *company*, not the role: a great-sounding title at a company below my floor
is still a no. Rewrite these anchors to match your own bar; the tier
score interpolates from them.

Anchors — interpolate for anything not listed:
- **90-100** — the strongest engineering orgs and top quant firms.
- **70-89** — strong product/infra companies, and well-funded startups with a real
  engineering reputation.
- **55-69** — solid engineering but not a step up: mid-size tech, established SaaS,
  big-tech-adjacent enterprises with real but unremarkable engineering.
- **30-54** — non-tech enterprises with an IT department, banks outside their quant desks,
  defense primes, hardware companies hiring generalist software staff.
- **0-29** — consultancies and staffing/outsourcing firms, agencies, and body shops.
  I will not apply to these.

Quant desks *inside* a large bank rate on the desk's reputation, not the bank's
retail arm.

## Location preference (soft, in priority order)
- SF Bay Area, New York, Austin, Remote. (Other places are fine, ranked slightly lower.)
