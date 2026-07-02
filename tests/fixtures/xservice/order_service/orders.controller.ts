import { Controller, Get, Param } from '@nestjs/common';

// order_service — NestJS producer AND consumer. Owns /orders/*, and calls
// user_service (Python) + billing_service (Java) cross-service, plus one
// external (Stripe) call that must NOT match any internal endpoint.
@Controller('orders')
export class OrdersController {
  @Get(':id')
  async getOrder(@Param('id') id: string) {
    const user = await fetch(`http://user-service/users/${id}`);
    const invoice = await fetch(`http://billing/invoices/${id}`);
    const charge = await fetch('https://api.stripe.com/v1/charges');
    return { user, invoice, charge };
  }
}
