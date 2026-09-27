// Ada's mark: the differential pair. Paths copied from site/assets/mark.svg (the app icon's geometry,
// app/src-tauri/icons/gen_app_icon.py), drawn in currentColor.
export function Mark({ className }: { className?: string }) {
  return (
    <svg
      viewBox="20 110 472 292"
      fill="none"
      stroke="currentColor"
      strokeWidth={50}
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
      className={className}
    >
      <path d="M42 293 H157 L311 139 H470" />
      <path d="M42 373 H190.1 L344.1 219 H470" />
    </svg>
  );
}
