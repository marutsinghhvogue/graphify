import { Injectable } from '@nestjs/common';
import { Cron, Interval } from '@nestjs/schedule';
import { EventPattern } from '@nestjs/microservices';

@Injectable()
export class CleanupService {
  @Cron('0 3 * * *')
  async purgeStaleOrders() {
    // NestJS @Cron trigger — 03:00 daily.
    return this.purge();
  }

  @Interval(60000)
  heartbeat() {
    return true;
  }

  @EventPattern('order.created')
  async onOrderCreated(data) {
    return this.purge();
  }

  async purge() {
    return 0;
  }
}
