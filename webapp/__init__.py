"""Web layer: FastAPI API + static frontend on top of the existing pipeline.

This package only *drives* the pipeline (submit → watch → approve/reject → history);
all extraction/validation/routing logic stays in expense_extractor/, agents/, workflow/.
"""
