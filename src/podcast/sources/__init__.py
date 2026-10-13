from .arxiv import Arxiv
from .biorxiv import BioMedRxiv
from .europepmc import EuropePMC
from .openalex import OpenAlex

REGISTRY = {s.name: s for s in (EuropePMC(), OpenAlex(), Arxiv(), BioMedRxiv())}
