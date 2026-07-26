package orders;

import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.stereotype.Service;

@Service
public class OrderService {

    @Autowired
    private InventoryClient inventoryClient;

    public void placeOrder() {
        inventoryClient.reserve();
    }
}
