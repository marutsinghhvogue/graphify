import { Injectable } from '@nestjs/common';
import { Cron, Interval } from '@nestjs/schedule';

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

  async purge() {
    return 0;
  }
}
