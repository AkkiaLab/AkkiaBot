// Named aliases for the generated OpenAPI types (`just gen-api` regenerates schema.d.ts).
import type { components } from "./schema";

type Schemas = components["schemas"];

export type CaptchaVerifyRequest = Schemas["CaptchaVerifyRequest"];
export type CaptchaVerifyResponse = Schemas["CaptchaVerifyResponse"];
export type CaptchaError = Schemas["CaptchaError"];
export type CaptchaErrorCode = CaptchaError["error"];
