export type PdffSequenceKey = "f" | "w" | "in" | "opp";

type PipelineOptions<TVolume, TReference> = {
  loadVolume: (sequence: PdffSequenceKey) => Promise<TVolume>;
  loadReference: () => Promise<TReference>;
  onPrimaryReady: (reference: TReference) => void | Promise<void>;
};

export async function runPdffLoadPipeline<TVolume, TReference>({
  loadVolume,
  loadReference,
  onPrimaryReady,
}: PipelineOptions<TVolume, TReference>) {
  const [, reference] = await Promise.all([
    Promise.all((["f", "w"] as PdffSequenceKey[]).map(loadVolume)),
    loadReference(),
  ]);
  await onPrimaryReady(reference);
  const secondaryResults = await Promise.allSettled(
    (["in", "opp"] as PdffSequenceKey[]).map(loadVolume),
  );
  return { reference, secondaryResults };
}
