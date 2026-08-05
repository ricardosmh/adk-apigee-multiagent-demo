from sqlalchemy import Column, Integer, String, Numeric, Text
from app.database import Base

class Product(Base):
    __tablename__ = "products"

    productId = Column(Integer, primary_key=True, index=True, autoincrement=True)
    productName = Column(String(255), nullable=False)
    price = Column(Numeric(10, 2), nullable=False)
    description = Column(Text, nullable=True)
    SKU = Column(String(100), unique=True, index=True, nullable=False)
    category = Column(String(100), index=True, nullable=True)
    brand = Column(String(100), index=True, nullable=True)
    stockQuantity = Column(Integer, default=0, nullable=True)
    imageUrl = Column(String(255), nullable=True)
    
    dimension_height = Column(String(20), nullable=True)
    dimension_width = Column(String(20), nullable=True)
    dimension_depth = Column(String(20), nullable=True)
    weight = Column(String(20), nullable=True)
