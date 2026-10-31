# user_service

Owns customer accounts and identity. Responsible for registration, profile
management, and looking up a user by id. The source of truth for who a customer
is; other services reference users but never mutate them here.

Capabilities: sign up a new customer, update contact details, fetch a user
profile, deactivate an account.
