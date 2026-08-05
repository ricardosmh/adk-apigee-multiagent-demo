from sqlalchemy import Column, Integer, String, Numeric, Date, ForeignKey
from sqlalchemy.orm import relationship
from app.database import Base

class Order(Base):
    __tablename__ = "orders"

    orderId = Column(Integer, primary_key=True, index=True, autoincrement=True)
    orderDate = Column(Date, nullable=True)
    totalAmount = Column(Numeric(10, 2), nullable=True)
    customerId = Column(Integer, index=True, nullable=True)
    orderStatus = Column(String(100), default="pending", nullable=True)
    trackingNumber = Column(String(255), nullable=True)

    items = relationship("OrderItem", back_populates="order", cascade="all, delete-orphan")


class OrderItem(Base):
    __tablename__ = "order_items"

    id = Column(Integer, primary_key=True, index=True, autoincrement=True)
    orderId = Column(Integer, ForeignKey("orders.orderId"), index=True, nullable=True)
    productId = Column(Integer, index=True, nullable=True)
    quantity = Column(Integer, nullable=True)
    price = Column(Numeric(10, 2), nullable=True)

    order = relationship("Order", back_populates="items")


# Fallback Product model for direct DB price lookup when PRODUCTS_SERVICE_URL is not set
class Product(Base):
    __tablename__ = "products"
    __table_args__ = {'extend_existing': True}

    productId = Column(Integer, primary_key=True, index=True)
    price = Column(Numeric(10, 2), nullable=False)
