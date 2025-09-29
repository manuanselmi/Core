from app.models import db, Customer

class CustomerService:
    @staticmethod
    def get(customer_id: int) -> dict:
        customer = db.session.query(Customer).filter(Customer.id == customer_id).first()
        if not customer:
            return {"error": "Cliente no encontrado"}
        return {"id": customer.id, "phone": customer.phone, "name": customer.name}

    @staticmethod
    def find_or_create(phone: str, name: str | None = None, accept_terms: bool = False) -> dict:
        customer = db.session.query(Customer).filter(Customer.phone == phone).first()
        if customer:
            return {"id": customer.id, "phone": customer.phone, "name": customer.name, "accept_terms": customer.accept_terms}

        alias = name or "Amigo"
        new_customer = Customer(phone=phone, name=alias, accept_terms=accept_terms)
        db.session.add(new_customer)
        db.session.commit()
        return {"id": new_customer.id, "phone": new_customer.phone, "name": new_customer.name, "accept_terms": new_customer.accept_terms}

