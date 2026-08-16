# billing_service

Owns money movement and invoicing. Responsible for generating an invoice for an
order, calculating the amount due including tax, capturing payment, and issuing
refunds. The system of record for what a customer was charged.

Capabilities: create an invoice, fetch an invoice, compute totals and tax,
record a payment, refund a charge. Any change to pricing, tax, or payment
handling lives here.
