import { Suspense } from "react";
import { LoginForm } from "@/components/auth/LoginForm";

export default function LoginPage() {
  return (
    <Suspense
      fallback={
        <div className="flex min-h-[70vh] items-center justify-center font-mono text-sm text-mist-400">
          Loading…
        </div>
      }
    >
      <LoginForm />
    </Suspense>
  );
}
