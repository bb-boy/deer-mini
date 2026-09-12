import type { SVGAttributes } from "react";

export type IconName =
  | "activity"
  | "arrow-up"
  | "chevron-down"
  | "chevron-right"
  | "clock"
  | "close"
  | "copy"
  | "download"
  | "file"
  | "folder"
  | "menu"
  | "message"
  | "panel"
  | "paperclip"
  | "plus"
  | "search"
  | "settings"
  | "sparkles"
  | "stop"
  | "upload"
  | "user"
  | "workspace";

const ICON_PATHS: Record<IconName, readonly string[]> = {
  activity: [
    "M4 12h3l2-7 4 14 2-7h5",
  ],
  "arrow-up": ["M12 19V5", "m5 12 7-7 7 7"],
  "chevron-down": ["m6 9 6 6 6-6"],
  "chevron-right": ["m9 18 6-6-6-6"],
  clock: [
    "M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18Z",
    "M12 7v5l3 2",
  ],
  close: ["M6 6l12 12", "M18 6 6 18"],
  copy: [
    "M8 8h11v12H8z",
    "M5 16H4a1 1 0 0 1-1-1V4a1 1 0 0 1 1-1h11a1 1 0 0 1 1 1v1",
  ],
  download: ["M12 3v12", "m7 10 5 5 5-5", "M5 21h14"],
  file: [
    "M6 3h8l4 4v14H6z",
    "M14 3v5h5",
    "M9 13h6",
    "M9 17h5",
  ],
  folder: [
    "M3 6.5A2.5 2.5 0 0 1 5.5 4H10l2 2h6.5A2.5 2.5 0 0 1 21 8.5v8A2.5 2.5 0 0 1 18.5 19h-13A2.5 2.5 0 0 1 3 16.5z",
  ],
  menu: ["M4 6h16", "M4 12h16", "M4 18h16"],
  message: [
    "M20 11.5a7.5 7.5 0 0 1-8 7.5 8.6 8.6 0 0 1-3-.5L4 20l1.5-4A7.4 7.4 0 0 1 4 11.5 7.5 7.5 0 0 1 12 4a7.5 7.5 0 0 1 8 7.5Z",
  ],
  panel: [
    "M4 5h16v14H4z",
    "M9 5v14",
    "M6.5 9h.01",
    "M6.5 12h.01",
    "M6.5 15h.01",
  ],
  paperclip: [
    "m20.5 11.5-7.9 7.9a5 5 0 0 1-7.1-7.1l8.6-8.6a3.4 3.4 0 0 1 4.8 4.8l-8.6 8.6a1.8 1.8 0 0 1-2.5-2.5l7.9-7.9",
  ],
  plus: ["M12 5v14", "M5 12h14"],
  search: ["m20 20-4.5-4.5", "M11 18a7 7 0 1 0 0-14 7 7 0 0 0 0 14Z"],
  settings: [
    "M12 15.5a3.5 3.5 0 1 0 0-7 3.5 3.5 0 0 0 0 7Z",
    "M19.4 15a1.7 1.7 0 0 0 .3 1.9l.1.1-1.8 1.8-.1-.1a1.7 1.7 0 0 0-1.9-.3 1.7 1.7 0 0 0-1 1.5v.2h-2.6v-.2a1.7 1.7 0 0 0-1-1.5 1.7 1.7 0 0 0-1.9.3l-.1.1-1.8-1.8.1-.1A1.7 1.7 0 0 0 8 15a1.7 1.7 0 0 0-1.5-1H6.3v-2.6h.2a1.7 1.7 0 0 0 1.5-1 1.7 1.7 0 0 0-.3-1.9l-.1-.1 1.8-1.8.1.1a1.7 1.7 0 0 0 1.9.3 1.7 1.7 0 0 0 1-1.5v-.2H15v.2a1.7 1.7 0 0 0 1 1.5 1.7 1.7 0 0 0 1.9-.3l.1-.1 1.8 1.8-.1.1a1.7 1.7 0 0 0-.3 1.9 1.7 1.7 0 0 0 1.5 1h.2V14h-.2a1.7 1.7 0 0 0-1.5 1Z",
  ],
  sparkles: [
    "m12 3 1.3 4.7L18 9l-4.7 1.3L12 15l-1.3-4.7L6 9l4.7-1.3z",
    "m19 15 .7 2.3L22 18l-2.3.7L19 21l-.7-2.3L16 18l2.3-.7z",
  ],
  stop: ["M7 7h10v10H7z"],
  upload: ["M12 16V4", "m7 9 5-5 5 5", "M5 20h14"],
  user: [
    "M19 21a7 7 0 0 0-14 0",
    "M12 13a4 4 0 1 0 0-8 4 4 0 0 0 0 8Z",
  ],
  workspace: [
    "M4 4h16v16H4z",
    "M8 8h8",
    "M8 12h8",
    "M8 16h5",
  ],
};

export interface IconProps extends SVGAttributes<SVGSVGElement> {
  name: IconName;
  size?: number;
}

export function Icon({
  name,
  size = 18,
  strokeWidth = 1.8,
  ...props
}: IconProps) {
  return (
    <svg
      aria-hidden="true"
      fill="none"
      focusable="false"
      height={size}
      viewBox="0 0 24 24"
      width={size}
      stroke="currentColor"
      strokeLinecap="round"
      strokeLinejoin="round"
      strokeWidth={strokeWidth}
      {...props}
    >
      {ICON_PATHS[name].map((path, index) => (
        <path key={`${name}-${index}`} d={path} />
      ))}
    </svg>
  );
}
