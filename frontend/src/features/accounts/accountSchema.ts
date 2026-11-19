import { z } from "zod";
import { SUPPORTED_CURRENCIES } from "@/lib/money";

// Mirrors AccountCreate in app/schemas/account.py (name stripped, 1-100 chars; USD only).
export const createAccountSchema = z.object({
  name: z.string().trim().min(1, "Enter an account name").max(100, "At most 100 characters"),
  currency: z.enum(SUPPORTED_CURRENCIES),
});
