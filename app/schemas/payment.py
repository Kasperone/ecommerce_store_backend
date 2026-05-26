from app.schemas.base import BaseSchema


class PayUCheckoutResponse(BaseSchema):
    """Where to send the buyer to pay — PayU's hosted payment page."""

    redirect_uri: str
