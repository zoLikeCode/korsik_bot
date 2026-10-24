import { useState } from "react";
import { apiSource } from "./api";

export default function EmployeeAvatar({
  name,
  src,
  small = false,
}: {
  name: string;
  src?: string | null;
  small?: boolean;
}) {
  const [failed, setFailed] = useState<string | null>(null);
  const photo = apiSource(src, "");
  const initials = name.trim().split(/\s+/).slice(0, 2).map((part) => part[0]).join("") || "С";
  return (
    <span className={`avatar employee-avatar${small ? " small" : ""}`} aria-hidden="true">
      {photo && failed !== photo ? (
        <img src={photo} alt="" onError={() => setFailed(photo)} />
      ) : initials}
    </span>
  );
}
