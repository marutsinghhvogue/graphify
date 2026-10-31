# order_service

Owns the ordering workflow. Responsible for placing an order, tracking its
status through fulfilment, and assembling an order summary by pulling the
customer profile from user_service and the charge/invoice from billing_service.

Capabilities: create an order, look up an order, cancel an order, list a
customer's order history. Orchestrates checkout across the customer and billing
services.
