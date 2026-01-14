"""
Linka Product Service - Supabase Integrated
Handles product catalog, categories, and search
"""

from fastapi import FastAPI, HTTPException, Depends, Query
from pydantic import BaseModel, Field
from typing import Optional, List
from decimal import Decimal
import os
import logging

from shared.supabase_client import get_supabase_client
from shared.auth_middleware import (
    get_current_user,
    get_current_user_optional,
    AuthenticatedUser,
    UserRole,
    require_roles
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(
    title="Linka Product Service",
    description="Product catalog and search",
    version="2.0.0"
)

# ============ MODELS ============

class ProductCreate(BaseModel):
    category_id: Optional[str] = None
    name: str = Field(..., min_length=1, max_length=255)
    description: Optional[str] = None
    short_description: Optional[str] = None
    price: Decimal = Field(..., gt=0)
    compare_at_price: Optional[Decimal] = None
    sku: Optional[str] = None
    tags: Optional[List[str]] = None
    is_featured: bool = False

class ProductUpdate(BaseModel):
    category_id: Optional[str] = None
    name: Optional[str] = None
    description: Optional[str] = None
    price: Optional[Decimal] = None
    status: Optional[str] = None
    is_featured: Optional[bool] = None
    tags: Optional[List[str]] = None

class ProductVariantCreate(BaseModel):
    name: str
    sku: Optional[str] = None
    price: Decimal = Field(..., gt=0)
    option1_name: Optional[str] = None
    option1_value: Optional[str] = None
    option2_name: Optional[str] = None
    option2_value: Optional[str] = None

# ============ HEALTH CHECK ============

@app.get("/health")
async def health():
    return {"status": "alive", "service": "product-service", "version": "2.0.0"}

@app.get("/ready")
async def readiness():
    try:
        client = get_supabase_client()
        is_healthy = await client.health_check()
        if not is_healthy:
            return {"status": "not ready"}, 503
        return {"status": "ready", "service": "product-service"}
    except Exception as e:
        return {"status": "not ready", "detail": str(e)}, 503

# ============ CATEGORY ENDPOINTS ============

@app.get("/categories")
async def list_categories():
    """List all active categories"""
    try:
        client = get_supabase_client()
        
        categories = await client.query(
            table="categories",
            filters={"is_active": True},
            order="display_order.asc"
        )
        
        return {"categories": categories}
        
    except Exception as e:
        logger.error(f"Failed to list categories: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/categories/{category_id}")
async def get_category(category_id: str):
    """Get category with subcategories"""
    try:
        client = get_supabase_client()
        
        category = await client.query(
            table="categories",
            filters={"id": category_id},
            single=True
        )
        
        if not category:
            raise HTTPException(status_code=404, detail="Category not found")
        
        # Get subcategories
        subcategories = await client.query(
            table="categories",
            filters={"parent_id": category_id, "is_active": True}
        )
        
        category["subcategories"] = subcategories
        
        return category
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to get category: {e}")
        raise HTTPException(status_code=500, detail=str(e))

# ============ PRODUCT ENDPOINTS ============

@app.get("/products")
async def list_products(
    category_id: Optional[str] = None,
    retailer_id: Optional[str] = None,
    search: Optional[str] = None,
    min_price: Optional[float] = None,
    max_price: Optional[float] = None,
    is_featured: Optional[bool] = None,
    limit: int = Query(default=20, le=100),
    offset: int = 0,
    user: Optional[AuthenticatedUser] = Depends(get_current_user_optional)
):
    """
    List products with optional filters.
    Public endpoint - shows active products only.
    """
    try:
        client = get_supabase_client()
        
        # Use search function if search query provided
        if search:
            products = await client.rpc(
                "search_products",
                {
                    "p_query": search,
                    "p_category_id": category_id,
                    "p_retailer_id": retailer_id,
                    "p_min_price": min_price,
                    "p_max_price": max_price,
                    "p_limit": limit,
                    "p_offset": offset
                }
            )
        else:
            # Build filters
            filters = {"status": "active"}
            
            if category_id:
                filters["category_id"] = category_id
            if retailer_id:
                filters["retailer_id"] = retailer_id
            if is_featured is not None:
                filters["is_featured"] = is_featured
            
            products = await client.query(
                table="products",
                select="*, categories(name, slug), product_images(url, is_primary)",
                filters=filters,
                order="created_at.desc",
                limit=limit,
                offset=offset
            )
        
        return {
            "products": products,
            "count": len(products),
            "limit": limit,
            "offset": offset
        }
        
    except Exception as e:
        logger.error(f"Failed to list products: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/products/{product_id}")
async def get_product(product_id: str):
    """Get product with variants and images"""
    try:
        client = get_supabase_client()
        
        product = await client.query(
            table="products",
            select="*, categories(name, slug), product_images(*), product_variants(*)",
            filters={"id": product_id},
            single=True
        )
        
        if not product:
            raise HTTPException(status_code=404, detail="Product not found")
        
        # Get inventory info
        inventory = await client.query(
            table="inventory",
            select="available_quantity, warehouse_id",
            filters={"product_id": product_id}
        )
        
        product["total_available"] = sum(inv.get("available_quantity", 0) for inv in inventory)
        product["in_stock"] = product["total_available"] > 0
        
        return product
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to get product: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/products")
async def create_product(
    product: ProductCreate,
    user: AuthenticatedUser = Depends(require_roles([UserRole.RETAILER, UserRole.ADMIN]))
):
    """Create a new product (retailers only)"""
    logger.info(f"Creating product for retailer: {user.id}")
    
    try:
        client = get_supabase_client()
        
        # Generate slug from name
        slug = product.name.lower().replace(" ", "-")
        
        product_data = {
            "retailer_id": user.id,
            "category_id": product.category_id,
            "name": product.name,
            "slug": slug,
            "description": product.description,
            "short_description": product.short_description,
            "price": float(product.price),
            "compare_at_price": float(product.compare_at_price) if product.compare_at_price else None,
            "sku": product.sku,
            "tags": product.tags,
            "is_featured": product.is_featured,
            "status": "draft"
        }
        
        created = await client.insert(table="products", data=product_data)
        
        logger.info(f"Product created: {created[0]['id']}")
        
        return {
            "id": created[0]["id"],
            "slug": created[0]["slug"],
            "message": "Product created successfully"
        }
        
    except Exception as e:
        logger.error(f"Failed to create product: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.patch("/products/{product_id}")
async def update_product(
    product_id: str,
    update: ProductUpdate,
    user: AuthenticatedUser = Depends(require_roles([UserRole.RETAILER, UserRole.ADMIN]))
):
    """Update a product"""
    try:
        client = get_supabase_client()
        
        # Verify ownership
        existing = await client.query(
            table="products",
            filters={"id": product_id},
            single=True
        )
        
        if not existing:
            raise HTTPException(status_code=404, detail="Product not found")
        
        if user.role != UserRole.ADMIN and existing["retailer_id"] != user.id:
            raise HTTPException(status_code=403, detail="Access denied")
        
        update_data = update.model_dump(exclude_unset=True)
        
        if not update_data:
            raise HTTPException(status_code=400, detail="No fields to update")
        
        # Convert Decimal to float for JSON
        if "price" in update_data:
            update_data["price"] = float(update_data["price"])
        
        updated = await client.update(
            table="products",
            data=update_data,
            filters={"id": product_id}
        )
        
        return {
            "id": product_id,
            "message": "Product updated successfully"
        }
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to update product: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.delete("/products/{product_id}")
async def delete_product(
    product_id: str,
    user: AuthenticatedUser = Depends(require_roles([UserRole.RETAILER, UserRole.ADMIN]))
):
    """Archive a product (soft delete)"""
    try:
        client = get_supabase_client()
        
        existing = await client.query(
            table="products",
            filters={"id": product_id},
            single=True
        )
        
        if not existing:
            raise HTTPException(status_code=404, detail="Product not found")
        
        if user.role != UserRole.ADMIN and existing["retailer_id"] != user.id:
            raise HTTPException(status_code=403, detail="Access denied")
        
        # Soft delete by changing status
        await client.update(
            table="products",
            data={"status": "archived"},
            filters={"id": product_id}
        )
        
        return {"message": "Product archived successfully"}
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to delete product: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/products/{product_id}/variants")
async def add_product_variant(
    product_id: str,
    variant: ProductVariantCreate,
    user: AuthenticatedUser = Depends(require_roles([UserRole.RETAILER, UserRole.ADMIN]))
):
    """Add a variant to a product"""
    try:
        client = get_supabase_client()
        
        # Verify ownership
        product = await client.query(
            table="products",
            filters={"id": product_id},
            single=True
        )
        
        if not product:
            raise HTTPException(status_code=404, detail="Product not found")
        
        if user.role != UserRole.ADMIN and product["retailer_id"] != user.id:
            raise HTTPException(status_code=403, detail="Access denied")
        
        variant_data = {
            "product_id": product_id,
            "name": variant.name,
            "sku": variant.sku,
            "price": float(variant.price),
            "option1_name": variant.option1_name,
            "option1_value": variant.option1_value,
            "option2_name": variant.option2_name,
            "option2_value": variant.option2_value
        }
        
        created = await client.insert(table="product_variants", data=variant_data)
        
        return {
            "id": created[0]["id"],
            "message": "Variant added successfully"
        }
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to add variant: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ============ RETAILER PRODUCTS ============

@app.get("/retailer/products")
async def list_retailer_products(
    status: Optional[str] = None,
    limit: int = 20,
    offset: int = 0,
    user: AuthenticatedUser = Depends(require_roles([UserRole.RETAILER]))
):
    """List products for the authenticated retailer (includes drafts)"""
    try:
        client = get_supabase_client()
        
        filters = {"retailer_id": user.id}
        if status:
            filters["status"] = status
        
        products = await client.query(
            table="products",
            select="*, product_images(url, is_primary)",
            filters=filters,
            order="created_at.desc",
            limit=limit,
            offset=offset
        )
        
        return {
            "products": products,
            "count": len(products)
        }
        
    except Exception as e:
        logger.error(f"Failed to list retailer products: {e}")
        raise HTTPException(status_code=500, detail=str(e))


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
