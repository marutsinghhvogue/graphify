package com.example.billing;

import org.springframework.web.bind.annotation.*;

// billing_service — Spring producer. Owns the /invoices/* path namespace.
@RestController
@RequestMapping("/invoices")
public class InvoiceController {

    @GetMapping("/{id}")
    public Invoice getInvoice(@PathVariable String id) {
        return new Invoice(id);
    }
}
