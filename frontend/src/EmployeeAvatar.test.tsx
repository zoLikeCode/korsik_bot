import { describe, expect, it } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import EmployeeAvatar from "./EmployeeAvatar";

describe("employee avatar source boundary", () => {
  it("renders an authorized internal photo URL without adding another name announcement", () => {
    const html = renderToStaticMarkup(
      <EmployeeAvatar
        name="Анна Директор"
        src="/api/v1/organization/employees/employee-one/photo"
      />,
    );
    expect(html).toContain(
      'src="/api/v1/organization/employees/employee-one/photo"',
    );
    expect(html).toContain('aria-hidden="true"');
    expect(html).toContain('alt=""');
    expect(html).not.toContain("Анна Директор");
  });
  it.each([
    "https://external.example/photo.jpg",
    "javascript:alert(1)",
    "/api/v1/photo\nInjected",
    null,
  ])(
    "falls back to initials for an unavailable or non-internal photo: %s",
    (src) => {
      const html = renderToStaticMarkup(
        <EmployeeAvatar name="  Анна   Директор  " src={src} />,
      );
      expect(html).toContain("АД");
      expect(html).not.toContain("<img");
    },
  );
  it("keeps a stable fallback for an empty employee name", () => {
    const html = renderToStaticMarkup(<EmployeeAvatar name="   " />);
    expect(html).toContain("С");
    expect(html).not.toContain("<img");
  });
});
