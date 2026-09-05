export function Icon({ name, className = "" }) {
  const common = {
    className,
    viewBox: "0 0 24 24",
    focusable: "false",
    "aria-hidden": "true"
  };

  switch (name) {
    case "arrow-up":
      return (
        <svg {...common}>
          <path d="M12 19V5" />
          <path d="m5 12 7-7 7 7" />
        </svg>
      );
    case "bar-chart":
      return (
        <svg {...common}>
          <path d="M5 20V10" />
          <path d="M12 20V4" />
          <path d="M19 20v-7" />
        </svg>
      );
    case "clock":
      return (
        <svg {...common}>
          <circle cx="12" cy="12" r="8" />
          <path d="M12 8v5l3 2" />
        </svg>
      );
    case "message":
      return (
        <svg {...common}>
          <path d="M5 6.5A5 5 0 0 1 10 2h4a5 5 0 0 1 5 5v3.5a5 5 0 0 1-5 5h-3.5L6 20v-5.2a5 5 0 0 1-1-3V6.5Z" />
          <path d="M9 9.5h.01" />
          <path d="M12 9.5h.01" />
          <path d="M15 9.5h.01" />
        </svg>
      );
    case "plus":
      return (
        <svg {...common}>
          <path d="M12 5v14" />
          <path d="M5 12h14" />
        </svg>
      );
    case "search":
      return (
        <svg {...common}>
          <circle cx="11" cy="11" r="7" />
          <path d="m16 16 4 4" />
        </svg>
      );
    case "shield":
      return (
        <svg {...common}>
          <path d="M12 3.5 18.5 6v5.4c0 4.1-2.6 7.8-6.5 9.1-3.9-1.3-6.5-5-6.5-9.1V6L12 3.5Z" />
          <path d="m9 12 2 2 4-4" />
        </svg>
      );
    case "sprout":
      return (
        <svg {...common}>
          <path d="M12 20V10" />
          <path d="M12 10C9 7 6 6.5 4 7c.5 4 3.5 6 8 3Z" />
          <path d="M12 10c3-3 6-3.5 8-3-.5 4-3.5 6-8 3Z" />
        </svg>
      );
    case "trend":
    default:
      return (
        <svg {...common}>
          <path d="M4 16.5 9.2 11.3l3.5 3.5L20 7.5" />
          <path d="M14.5 7.5H20v5.5" />
        </svg>
      );
  }
}
