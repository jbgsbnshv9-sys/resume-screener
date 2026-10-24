"""Synthetic personas for fixtures and eval sets (principle 7: no real resumes).

Personas are structured so counterfactual variants can change exactly one
attribute. Every name, employer, school and phone number here is invented;
phone numbers use the reserved 555-01xx range and emails use example.com.
"""

from __future__ import annotations

import random
from typing import Literal

from pydantic import BaseModel, Field

MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]

# Name lists associated with different demographic groups, in the style of
# resume audit studies. Used only to build counterfactual pairs.
NAME_GROUPS: dict[str, dict[str, list[str]]] = {
    "group_a": {"first": ["Emily", "Greg", "Anne", "Brad", "Claire", "Todd"], "last": ["Walsh", "Baker", "Sullivan", "Murphy", "Kelly", "Ryan"]},
    "group_b": {"first": ["Lakisha", "Jamal", "Tamika", "Darnell", "Ebony", "Tyrone"], "last": ["Washington", "Jefferson", "Booker", "Banks", "Mosley", "Jackson"]},
    "group_c": {"first": ["Sofia", "Alejandro", "Lucia", "Mateo", "Valentina", "Diego"], "last": ["Hernandez", "Ramirez", "Gutierrez", "Morales", "Castillo", "Flores"]},
    "group_d": {"first": ["Wei", "Mei", "Jun", "Yan", "Hao", "Lin"], "last": ["Zhang", "Wang", "Liu", "Chen", "Huang", "Zhao"]},
    "group_e": {"first": ["Priya", "Arjun", "Ananya", "Rohan", "Kavya", "Vikram"], "last": ["Sharma", "Patel", "Iyer", "Reddy", "Nair", "Gupta"]},
    "group_f": {"first": ["Fatima", "Omar", "Layla", "Yusuf", "Amira", "Karim"], "last": ["Haddad", "Rahman", "Khalil", "Nasser", "Saleh", "Aziz"]},
}

ORGS = ["Northwind Logistics", "Bluefin Analytics", "Cedar Health Systems", "Quarry Labs", "Harbor Payments", "Lumen Retail Group", "Tessellate Software", "Pinecrest Media", "Orbital Freight", "Juniper Civic Tech"]
SCHOOLS = {
    "US": [("Lakeview State University", "Lakeview"), ("Eastbrook Institute of Technology", "Eastbrook"), ("Riverside Polytechnic", "Riverside")],
    "Nigeria": [("University of Ilaro Coast", "Lagos")],
    "India": [("Deccan Institute of Engineering", "Pune")],
    "Philippines": [("Visayas Technological University", "Cebu")],
    "Poland": [("Vistula Polytechnic", "Krakow")],
    "Brazil": [("Paulista Federal Institute", "Campinas")],
}
LOCATIONS = ["Springfield, IL", "Portland, OR", "Austin, TX", "Raleigh, NC", "Denver, CO"]

# Criterion-aligned bullet banks. Index = depth (1 = passing mention, 3 = deep ownership).
DOMAINS: dict[str, dict[int, list[str]]] = {
    "backend_development": {
        1: ["Fixed bugs in an internal Python reporting script used by the finance team."],
        2: [
            "Built and maintained REST API services in Python and Go handling customer orders in production.",
            "Implemented backend endpoints for account management in a Java service used by 40,000 users.",
        ],
        3: [
            "Designed and owned a production order-routing service in Go serving 3,000 requests per second, from design review through on-call.",
            "Led the rewrite of a monolithic Python backend into versioned API services, cutting p99 latency by 60%.",
        ],
    },
    "distributed_systems": {
        1: ["Read messages from a Kafka topic in a batch job."],
        2: ["Built event-driven consumers on Kafka with idempotent processing and retries across services."],
        3: [
            "Designed a distributed job scheduler with leader election and exactly-once delivery across three regions.",
            "Owned consistency and partitioning design for a replicated event stream processing 2 billion messages a day.",
        ],
    },
    "data_storage": {
        1: ["Wrote SQL queries to pull weekly reports."],
        2: ["Designed PostgreSQL schemas and indexes for a billing service and optimized slow queries."],
        3: [
            "Led a zero-downtime migration of a 4 TB PostgreSQL database to a sharded cluster, including schema redesign.",
            "Owned query performance for the primary datastore, profiling and rewriting queries to cut database load by 45%.",
        ],
    },
    "reliability_operations": {
        1: ["Joined the on-call rotation for an internal tool."],
        2: ["Added monitoring dashboards and alerts for backend services and responded to production incidents on-call."],
        3: ["Ran incident response and blameless postmortems for production outages, and built the deployment pipeline with automated rollback."],
    },
    "testing_quality": {
        1: ["Wrote a few unit tests for new features."],
        2: ["Added integration tests and contract tests to the CI pipeline, raising coverage of critical paths."],
        3: ["Built the team's testing strategy, including load tests and property-based tests, and enforced code review standards."],
    },
    "collaboration_mentorship": {
        1: ["Participated in sprint planning with product managers."],
        2: ["Mentored two junior engineers through code review and pairing sessions."],
        3: ["Mentored five engineers, ran the design review process, and coordinated cross-team technical planning."],
    },
}
DOMAIN_SKILLS = {
    "backend_development": ["Python", "Go", "Java"],
    "distributed_systems": ["Kafka", "gRPC"],
    "data_storage": ["PostgreSQL", "Redis"],
    "reliability_operations": ["Prometheus", "Terraform"],
    "testing_quality": ["pytest"],
    "collaboration_mentorship": [],
}
FILLER = ["Updated internal documentation for the team wiki.", "Attended weekly team meetings and demos.", "Supported customer escalations with the support team."]
TITLES = {1: "Software Engineer", 2: "Software Engineer II", 3: "Senior Software Engineer"}


class Job(BaseModel):
    title: str
    org: str
    start_year: int
    start_month: int
    end_year: int | None  # None = Present
    end_month: int | None
    bullets: list[str]


class School(BaseModel):
    institution: str
    city: str
    country: str
    degree: str
    field: str
    start_year: int
    end_year: int


class Persona(BaseModel):
    id: str
    first_name: str
    last_name: str
    name_group: str
    email: str
    phone: str
    location: str
    summary: str = ""
    jobs: list[Job]
    education: list[School]
    skills: list[str]
    certifications: list[str] = Field(default_factory=list)
    activities: list[str] = Field(default_factory=list)
    links: list[str] = Field(default_factory=list)
    # Generation-time truth used to derive the intended label; never shown to the pipeline.
    depths: dict[str, int] = Field(default_factory=dict)

    @property
    def full_name(self) -> str:
        return f"{self.first_name} {self.last_name}"


TierLabel = Literal["strong", "review", "weak"]

# Depth profiles per intended tier (sampled per domain).
_PROFILES: dict[str, list[tuple[int, ...]]] = {
    "strong": [(3, 3, 2, 2, 2, 2), (3, 2, 3, 2, 1, 2), (3, 3, 3, 1, 2, 1)],
    "review": [(2, 2, 1, 1, 1, 1), (2, 1, 2, 1, 1, 0), (3, 1, 1, 0, 1, 1), (2, 2, 2, 0, 0, 1)],
    "weak": [(1, 0, 1, 0, 1, 1), (1, 1, 0, 0, 0, 1), (0, 0, 1, 1, 1, 0), (1, 0, 0, 1, 0, 0)],
}


def _email(first: str, last: str, rng: random.Random) -> str:
    return f"{first.lower()}.{last.lower()}{rng.randint(1, 99)}@example.com"


def _phone(rng: random.Random) -> str:
    return f"(555) 01{rng.randint(0, 9)}{rng.randint(0, 9)}-{rng.randint(1000, 9999)}"


def generate_persona(persona_id: str, tier: TierLabel, rng: random.Random, name_group: str | None = None) -> Persona:
    group = name_group or rng.choice(sorted(NAME_GROUPS))
    first = rng.choice(NAME_GROUPS[group]["first"])
    last = rng.choice(NAME_GROUPS[group]["last"])
    profile = rng.choice(_PROFILES[tier])
    depths = dict(zip(DOMAINS, profile, strict=True))

    bullets: list[str] = []
    for domain, depth in depths.items():
        for d in range(1, depth + 1):
            bullets.extend(DOMAINS[domain][d])
    rng.shuffle(bullets)
    n_jobs = 2 if len(bullets) <= 6 else 3
    bullets.extend(rng.sample(FILLER, k=min(n_jobs, len(FILLER))))
    rng.shuffle(bullets)

    jobs: list[Job] = []
    year = 2026
    orgs = rng.sample(ORGS, k=n_jobs)
    seniority = max(1, min(3, max(profile)))
    for j in range(n_jobs):
        span = rng.randint(2, 4)
        start = year - span
        chunk = bullets[j::n_jobs]
        jobs.append(
            Job(
                title=TITLES[max(1, seniority - j)],
                org=orgs[j],
                start_year=start,
                start_month=rng.randint(1, 12),
                end_year=None if j == 0 else year,
                end_month=None if j == 0 else rng.randint(1, 12),
                bullets=chunk,
            )
        )
        year = start
    school_name, city = rng.choice(SCHOOLS["US"])
    grad = year - rng.randint(0, 1)
    education = [School(institution=school_name, city=city, country="USA", degree="B.S.", field=rng.choice(["Computer Science", "Computer Engineering", "Mathematics"]), start_year=grad - 4, end_year=grad)]
    skills = sorted({s for d, depth in depths.items() if depth > 0 for s in DOMAIN_SKILLS[d]} | {"Git", "Linux"})
    certs = ["AWS Certified Developer - Associate"] if depths["reliability_operations"] >= 2 and rng.random() < 0.5 else []
    return Persona(
        id=persona_id,
        first_name=first,
        last_name=last,
        name_group=group,
        email=_email(first, last, rng),
        phone=_phone(rng),
        location=rng.choice(LOCATIONS),
        jobs=jobs,
        education=education,
        skills=skills,
        certifications=certs,
        links=[f"github.com/{first.lower()}{last.lower()}"],
        depths=depths,
    )
