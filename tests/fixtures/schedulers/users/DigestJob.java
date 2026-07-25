package users;

import org.springframework.scheduling.annotation.Scheduled;
import org.springframework.stereotype.Component;

@Component
public class DigestJob {

    @Scheduled(cron = "0 0 8 * * *")
    public void sendDailyDigest() {
        // Spring @Scheduled cron trigger — 08:00 daily.
        buildDigest();
    }

    public void buildDigest() {
    }
}
