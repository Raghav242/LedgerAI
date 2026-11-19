import { zodResolver } from "@hookform/resolvers/zod";
import { Loader2 } from "lucide-react";
import { useForm } from "react-hook-form";
import type { z } from "zod";
import { FormField } from "@/components/common/FormField";
import { describedBy } from "@/lib/a11y";
import { describeError } from "@/lib/errors";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/misc";
import { NativeSelect } from "@/components/ui/native-select";
import { useCreateAccount } from "@/hooks/queries";
import { SUPPORTED_CURRENCIES } from "@/lib/money";
import { createAccountSchema } from "./accountSchema";
import type { Account } from "@/types/api";

type FormValues = z.infer<typeof createAccountSchema>;

export function CreateAccountForm({ onCreated }: { onCreated?: (account: Account) => void }) {
  const mutation = useCreateAccount();
  const form = useForm<FormValues>({
    resolver: zodResolver(createAccountSchema),
    defaultValues: { name: "", currency: "USD" },
  });
  const { errors } = form.formState;

  const onSubmit = form.handleSubmit(async (values) => {
    try {
      const account = await mutation.mutateAsync(values);
      form.reset({ name: "", currency: "USD" });
      onCreated?.(account);
    } catch {
      // shown from mutation.error below
    }
  });

  return (
    <form onSubmit={onSubmit} noValidate className="grid gap-4" aria-label="Create account">
      <FormField id="account-name" label="Account name" error={errors.name?.message} hint="For example: Alice Coffee">
        <Input
          id="account-name"
          autoComplete="off"
          placeholder="Alice Coffee"
          aria-invalid={!!errors.name}
          aria-describedby={describedBy("account-name", errors.name?.message, true)}
          {...form.register("name")}
        />
      </FormField>
      <FormField id="account-currency" label="Currency" hint="USD is the only currency the backend supports.">
        <NativeSelect id="account-currency" aria-describedby="account-currency-hint" {...form.register("currency")}>
          {SUPPORTED_CURRENCIES.map((c) => (
            <option key={c} value={c}>
              {c}
            </option>
          ))}
        </NativeSelect>
      </FormField>
      {mutation.error && (
        <Alert variant="destructive">
          <AlertTitle>{describeError(mutation.error).title}</AlertTitle>
          <AlertDescription>{describeError(mutation.error).message}</AlertDescription>
        </Alert>
      )}
      <div className="flex justify-end">
        <Button type="submit" disabled={mutation.isPending}>
          {mutation.isPending && <Loader2 className="animate-spin" />}
          Create account
        </Button>
      </div>
    </form>
  );
}
