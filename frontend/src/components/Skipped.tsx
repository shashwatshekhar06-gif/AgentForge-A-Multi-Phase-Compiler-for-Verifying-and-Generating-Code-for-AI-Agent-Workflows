// Shown when a phase did not run because an earlier phase failed.
export default function Skipped({ phase }: { phase: string }) {
  return (
    <div className="p-6 text-slate-500">
      The <b>{phase}</b> phase was skipped because an earlier phase reported errors. Fix the problems listed under the
      editor and compile again.
    </div>
  );
}
