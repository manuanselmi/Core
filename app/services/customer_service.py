import logging

logger = logging.getLogger("customer_service")


class CustomerService:
    @staticmethod
    def get_by_phone(phone: str, repo_provider=None) -> dict:
        """
        Obtiene un customer por teléfono.
        
        Args:
            phone: Número de teléfono
            repo_provider: RepositoryProvider (requerido)
            
        Returns:
            Dict con datos del customer o dict con error
        """
        if not repo_provider:
            return {"error": "repo_provider es requerido"}
        
        profile = repo_provider.customers.get_by_phone(phone)
        if not profile:
            return {"error": "Cliente no encontrado"}
        
        return {
            "phone": profile.get("phone"),
            "name": profile.get("name"),
            "accept_terms": profile.get("accept_terms", False),
        }
    
    @staticmethod
    def get(customer_id: int, repo_provider=None) -> dict:
        """
        DEPRECADO: En DynamoDB no hay customer_id numérico.
        Usar get_by_phone() en su lugar.
        """
        logger.warning(
            "[CustomerService] get(customer_id=%s) está deprecado en DynamoDB. "
            "Usar get_by_phone() en su lugar.",
            customer_id
        )
        return {"error": "get() por customer_id no soportado en DynamoDB. Usar get_by_phone()."}

    @staticmethod
    def find_or_create(phone: str, name: str | None = None, accept_terms: bool = False, repo_provider=None) -> dict:
        """
        Busca o crea un customer por teléfono.
        
        Args:
            phone: Número de teléfono
            name: Nombre del customer (opcional)
            accept_terms: Si aceptó términos y condiciones
            repo_provider: RepositoryProvider (requerido)
            
        Returns:
            Dict con datos del customer
        """
        if not repo_provider:
            return {"error": "repo_provider es requerido"}
        
        # Intentar obtener profile existente
        profile = repo_provider.customers.get_by_phone(phone)
        
        if profile:
            return {
                "phone": profile.get("phone"),
                "name": profile.get("name"),
                "accept_terms": profile.get("accept_terms", False),
            }
        
        # Crear nuevo profile
        alias = name or "Amigo"
        new_profile = {
            "phone": phone,
            "name": alias,
            "accept_terms": accept_terms,
        }
        repo_provider.customers.upsert_profile(phone, new_profile)
        
        return {
            "phone": phone,
            "name": alias,
            "accept_terms": accept_terms,
        }

