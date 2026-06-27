import re

# Coarse keyword patterns — match workout *type*, not rep count or duration.
# "3x3min LT" and "6x3min LT" both → "LT"; the per-rep structure lives in laps.
WORKOUT_LABEL_PATTERNS: list[tuple[str, str]] = [
    (r"\bflux\b", "MP Flux"),
    (r"\blt\b|sublt|\bsub.lt\b", "LT"),  # "@ LT", "subLT", "sub-LT"
    (r"\d+x\d+min\b", "LT"),              # Nx3min, Nx6min interval format
    (r"\d+x\d+s\b", "LT"),               # Nx70s interval format
    (r"\btempo\b", "Tempo"),
    (r"\bstride", "Strides"),
    (r"\bfartlek\b", "Fartlek"),
    (r"\bprogression\b", "Progression"),
    (r"\bhill", "Hills"),
]


def classify_workout(name: str) -> str | None:
    for pattern, label in WORKOUT_LABEL_PATTERNS:
        if re.search(pattern, name, re.IGNORECASE):
            return label
    return None
