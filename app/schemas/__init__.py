from app.schemas.auth import (
    EmailVerification,
    PasswordChange,
    PasswordReset,
    PasswordResetConfirm,
    Token,
    TokenData,
)
from app.schemas.banner import (
    BannerBase,
    BannerCreate,
    BannerListResponse,
    BannerPlacementLiteral,
    BannerResponse,
    BannerUpdate,
)
from app.schemas.base import BaseSchema, TimestampSchema
from app.schemas.category import (
    CategoryBase,
    CategoryCreate,
    CategoryResponse,
    CategoryUpdate,
    CategoryWithProductCount,
)
from app.schemas.coupon import (
    CouponBase,
    CouponCreate,
    CouponDiscountTypeLiteral,
    CouponListResponse,
    CouponResponse,
    CouponUpdate,
)
from app.schemas.order import (
    OrderBase,
    OrderCreate,
    OrderItemBase,
    OrderItemCreate,
    OrderItemResponse,
    OrderListItem,
    OrderListResponse,
    OrderResponse,
    OrderUpdate,
    OrderWithUser,
)
from app.schemas.product import (
    ProductBase,
    ProductCreate,
    ProductListResponse,
    ProductResponse,
    ProductUpdate,
    ProductWithCategory,
)
from app.schemas.refund import (
    CorrectiveInvoiceResponse,
    RefundCreate,
    RefundReasonLiteral,
    RefundResponse,
)
from app.schemas.report import (
    CategoryRevenueReport,
    CategoryRevenueRow,
    RefundSummary,
    RefundSummaryRow,
    SalesBucket,
    SalesGranularity,
    SalesReport,
    TopProductRow,
    TopProductsReport,
)
from app.schemas.staff import (
    StaffListResponse,
    StaffResponse,
    StaffUpdate,
)
from app.schemas.store_config import (
    StoreConfigBase,
    StoreConfigResponse,
    StoreConfigUpdate,
)
from app.schemas.user import (
    UserBase,
    UserCreate,
    UserInDB,
    UserLogin,
    UserResponse,
    UserUpdate,
)

__all__ = [
    # Base
    "BaseSchema",
    "TimestampSchema",
    # Banner
    "BannerBase",
    "BannerCreate",
    "BannerUpdate",
    "BannerResponse",
    "BannerListResponse",
    "BannerPlacementLiteral",
    # User
    "UserBase",
    "UserCreate",
    "UserUpdate",
    "UserResponse",
    "UserInDB",
    "UserLogin",
    # Category
    "CategoryBase",
    "CategoryCreate",
    "CategoryUpdate",
    "CategoryResponse",
    "CategoryWithProductCount",
    # Coupon
    "CouponBase",
    "CouponCreate",
    "CouponUpdate",
    "CouponResponse",
    "CouponListResponse",
    "CouponDiscountTypeLiteral",
    # Product
    "ProductBase",
    "ProductCreate",
    "ProductUpdate",
    "ProductResponse",
    "ProductWithCategory",
    "ProductListResponse",
    # Order
    "OrderItemBase",
    "OrderItemCreate",
    "OrderItemResponse",
    "OrderBase",
    "OrderCreate",
    "OrderUpdate",
    "OrderResponse",
    "OrderWithUser",
    "OrderListItem",
    "OrderListResponse",
    # Refund
    "RefundCreate",
    "RefundResponse",
    "RefundReasonLiteral",
    "CorrectiveInvoiceResponse",
    # Reports
    "SalesGranularity",
    "SalesBucket",
    "SalesReport",
    "TopProductRow",
    "TopProductsReport",
    "CategoryRevenueRow",
    "CategoryRevenueReport",
    "RefundSummaryRow",
    "RefundSummary",
    # Staff
    "StaffResponse",
    "StaffUpdate",
    "StaffListResponse",
    # Store config
    "StoreConfigBase",
    "StoreConfigUpdate",
    "StoreConfigResponse",
    # Auth
    "Token",
    "TokenData",
    "PasswordChange",
    "PasswordReset",
    "PasswordResetConfirm",
    "EmailVerification",
]
