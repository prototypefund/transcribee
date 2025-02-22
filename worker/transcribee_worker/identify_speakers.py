import logging

import automerge
import numpy as np
import numpy.typing as npt
import torch
from sklearn.cluster import AgglomerativeClustering
from speechbrain.pretrained import EncoderClassifier
from transcribee_proto.document import Document
from transcribee_worker.types import ProgressCallbackType
from transcribee_worker.util import alist, async_task

from .config import settings


async def identify_speakers(
    number_of_speakers: int | None,
    audio: npt.NDArray,
    doc: Document,
    progress_callback: ProgressCallbackType,
):
    def work(_queue):
        logging.info("Running Speaker Identification")

        if len(doc.children) == 0:
            return
        elif len(doc.children) == 1:
            doc.children[0].speaker = automerge.Text("1")
            return

        def time_to_sample(time: float | None):
            if time is None:
                raise ValueError("time may not be None")
            return max(min(int(time * settings.SAMPLE_RATE), len(audio)), 0)

        segments = [
            (
                min(
                    time_to_sample(child.children[0].start),
                    # we always use at least 0.1s,
                    # otherwise the fingerprinting model explodes sometimes
                    # since the start of the segment might be less than 0.1s
                    # from end of the audio, we use this as a safety
                    len(audio) - time_to_sample(0.1),
                ),
                max(
                    time_to_sample(child.children[-1].end),
                    # we always use at least 0.1s,
                    # otherwise the fingerprinting model explodes sometimes
                    time_to_sample(child.children[0].start + 0.1),
                ),
            )
            for child in doc.children
        ]

        classifier = EncoderClassifier.from_hparams(
            source="speechbrain/spkrec-ecapa-voxceleb",
            savedir=settings.MODELS_DIR / "speechbrain-spkrec-ecapa-voxceleb",
        )
        if classifier is None:
            raise ValueError("classifier is None")

        embeddings = []
        for i, (start, end) in enumerate(segments):
            progress_callback(
                step="generating speaker embeddings", progress=i / (len(segments) + 1)
            )
            wav = audio[start:end]
            wav_tensor = torch.tensor(wav[np.newaxis, :])
            embedding = classifier.encode_batch(wav_tensor)
            embeddings.append(embedding[0, 0].detach().numpy())

        progress_callback(
            step="clustering speaker embeddings",
            progress=len(segments) / (len(segments) + 1),
        )

        clustering = AgglomerativeClustering(
            compute_full_tree=True,  # type: ignore
            linkage="complete",
            n_clusters=number_of_speakers,  # type: ignore
            # distance_threshold curtesty of
            # https://huggingface.co/pyannote/speaker-diarization/blob/369ac1852c71759894a48c9bb1c6f499a54862fe/config.yaml#L15
            distance_threshold=0.7153 if number_of_speakers is None else None,
            metric="cosine",
        )
        clustering.fit(np.array(embeddings))

        # we now re-shuffle the labels so that the first occuring speaker is 1, the second is 2, ...
        label_map = {}
        for label in clustering.labels_:
            if label not in label_map:
                label_map[label] = str(len(label_map) + 1)

        for para, label in zip(doc.children, clustering.labels_):
            para.speaker = automerge.Text(label_map[label])

    await alist(aiter(async_task(work)))
