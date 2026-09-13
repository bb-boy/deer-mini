// 迁移自 DeerFlow frontend/src/lib/utils.ts；许可见 src/markdown/DEERFLOW-LICENSE。
import { clsx, type ClassValue } from "clsx";
import { twMerge } from "tailwind-merge";

export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs));
}
