from pydantic import BaseModel, Field


class PredictionRequest(BaseModel):
    NetworkConnections: int = Field(ge=0)
    ProcessName: str = Field(min_length=1, max_length=255)


class PredictionResponse(BaseModel):
    prediction: int
    confidence: float
    threat_type: str
    model_version: str