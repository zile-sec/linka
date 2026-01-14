"""Supabase client for shared use across services"""
import os
from typing import Optional, Dict, Any, List
from supabase import create_client, Client
import logging

logger = logging.getLogger(__name__)


class SupabaseClient:
    """Supabase client singleton for database operations"""
    
    _instance: Optional[Client] = None
    
    def __init__(self):
        if not SupabaseClient._instance:
            supabase_url = os.getenv("SUPABASE_URL")
            supabase_key = os.getenv("SUPABASE_KEY")
            
            if not supabase_url or not supabase_key:
                raise ValueError("SUPABASE_URL and SUPABASE_KEY must be set")
            
            SupabaseClient._instance = create_client(supabase_url, supabase_key)
            logger.info("Supabase client initialized")
    
    @property
    def client(self) -> Client:
        """Get the Supabase client instance"""
        if not SupabaseClient._instance:
            self.__init__()
        return SupabaseClient._instance
    
    def query(self, table: str, filters: Optional[Dict] = None, order_by: Optional[str] = None) -> List[Dict]:
        """Query a table"""
        query = self.client.table(table).select("*")
        
        if filters:
            for key, value in filters.items():
                query = query.eq(key, value)
        
        if order_by:
            query = query.order(order_by)
        
        response = query.execute()
        return response.data
    
    def get_single(self, table: str, filters: Dict) -> Optional[Dict]:
        """Get a single record"""
        query = self.client.table(table).select("*")
        
        for key, value in filters.items():
            query = query.eq(key, value)
        
        response = query.single().execute()
        return response.data if response.data else None
    
    def insert(self, table: str, data: Dict) -> Dict:
        """Insert a record"""
        response = self.client.table(table).insert(data).execute()
        return response.data[0] if response.data else {}
    
    def update(self, table: str, filters: Dict, data: Dict) -> Dict:
        """Update records"""
        query = self.client.table(table).update(data)
        
        for key, value in filters.items():
            query = query.eq(key, value)
        
        response = query.execute()
        return response.data[0] if response.data else {}
    
    def delete(self, table: str, filters: Dict) -> bool:
        """Delete records"""
        query = self.client.table(table).delete()
        
        for key, value in filters.items():
            query = query.eq(key, value)
        
        response = query.execute()
        return len(response.data) > 0
    
    def rpc(self, function_name: str, params: Optional[Dict] = None) -> Any:
        """Call a Supabase RPC function"""
        response = self.client.rpc(function_name, params or {}).execute()
        return response.data


def get_supabase_client() -> SupabaseClient:
    """Get Supabase client instance"""
    return SupabaseClient()
