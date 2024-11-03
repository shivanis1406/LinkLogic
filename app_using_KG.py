import numpy as np
from typing import List, Dict, Tuple, Set, Optional
from sentence_transformers import SentenceTransformer
import networkx as nx
import torch
import torch.nn.functional as F
from dataclasses import dataclass
from collections import defaultdict
from dotenv import load_dotenv
import os, requests
import random

load_dotenv()

groq_api_key = os.getenv('GROQ_API_KEY')

@dataclass
class Triple:
    """
    Represents a knowledge graph triple (head, relation, tail)
    Using dataclass for automatic implementation of __eq__, __hash__, etc.
    """
    head: str
    relation: str
    tail: str
    
    def __hash__(self):
        return hash((self.head, self.relation, self.tail))

class KnowledgeGraphRAG:
    def __init__(
        self,
        embedding_model: str = 'all-MiniLM-L6-v2',
        device: Optional[str] = None,
        seed: int = 42
        ):
        """
        Initialize RAG system with embedding model and empty knowledge graph
        
        Args:
            embedding_model: Name of the sentence-transformers model to use
            device: Device to run the model on ('cpu', 'cuda', etc.)
            seed: Random seed for reproducibility
        """
        # Set deterministic behavior across all libraries
        self._set_deterministic_settings(seed)

        # Determine device and initialize encoder
        self.device = device or ('cuda' if torch.cuda.is_available() else 'cpu')
        print(f"Using device: {self.device}")
        self.encoder = self._init_encoder(embedding_model)
        
        # Initialize graph and embedding storage
        self.knowledge_graph = nx.DiGraph()
        self.node_embeddings: Dict[str, torch.Tensor] = {}
        self.edge_embeddings: Dict[Tuple[str, str], torch.Tensor] = {}
        self.triple_to_edge: Dict[Triple, Tuple[str, str]] = {}

    def _set_deterministic_settings(self, seed: int) -> None:
        """
        Set all random seeds and ensure deterministic behavior
        Critical for reproducible results, especially on CPU
        """
        random.seed(seed)
        np.random.seed(seed)
        torch.manual_seed(seed)
        
        # Enable deterministic operations
        torch.use_deterministic_algorithms(True)
        
        # Set CUDA settings if available
        if torch.cuda.is_available():
            torch.cuda.manual_seed_all(seed)
            torch.backends.cudnn.deterministic = True
            torch.backends.cudnn.benchmark = False
        
        # Set hash seed for consistent dictionary ordering
        os.environ['PYTHONHASHSEED'] = str(seed)
        
    def _init_encoder(self, model_name: str):
        """
        Initialize the encoder with deterministic settings
        Ensures consistent behavior across runs
        """
        # Wrap encoder.encode with no_grad for determinism
        with torch.no_grad():
            encoder = SentenceTransformer(model_name, device=self.device)
            
            # Freeze parameters for consistency
            for param in encoder.parameters():
                param.requires_grad = False
            
            return encoder

    def _compute_embedding(self, text: str) -> torch.Tensor:
        """
        Compute embedding with deterministic operations
        Ensures consistent embeddings across runs
        """
        with torch.no_grad():
            # Normalize text for consistent processing
            text = ' '.join(text.lower().split())
            
            # Compute embedding
            embedding = self.encoder.encode(text, convert_to_tensor=True)
            
            # Ensure consistent numerical precision
            embedding = embedding.to(dtype=torch.float32)
            
            # Sort for consistent ordering
            embedding = torch.sort(embedding)[0]
            
            return embedding.to(self.device)
        
    def add_triple(self, head: str, relation: str, tail: str) -> None:
        """
        Add knowledge triple to graph and compute embeddings
        
        Args:
            head: Source node of the triple
            relation: Relationship between head and tail
            tail: Target node of the triple
        """
        try:
            triple = Triple(head, relation, tail)
            
            # Add to graph with deterministic ordering
            self.knowledge_graph.add_edge(head, tail, relation=relation)
            
            # Compute node embeddings if not already present
            for node in sorted([head, tail]):  # Sort for consistency
                if node not in self.node_embeddings:
                    self.node_embeddings[node] = self._compute_embedding(node)
                    
            # Compute edge embedding
            edge_text = f"{head} {relation} {tail}"
            edge_key = (head, tail)
            self.edge_embeddings[edge_key] = self._compute_embedding(edge_text)
            self.triple_to_edge[triple] = edge_key
            
        except Exception as e:
            raise ValueError(f"Failed to add triple: {e}")
        
    def retrieve_relevant_subgraph(
        self,
        query: str,
        top_k: int = 5,
        similarity_threshold: float = 0.5
    ) -> List[Triple]:
        """
        Retrieve relevant subgraph with deterministic ordering
        
        Args:
            query: Input query text
            top_k: Number of top similar triples to return
            similarity_threshold: Minimum similarity score threshold
        """
        if not self.edge_embeddings:
            return []
            
        # Normalize query
        query = ' '.join(query.lower().split())
        
        # Compute query embedding
        with torch.no_grad():
            query_embedding = self._compute_embedding(query)
        
        # Use ordered dictionary for consistent ordering
        from collections import OrderedDict
        similarities = OrderedDict()
        
        # Stack embeddings in deterministic order
        edge_keys = sorted(self.edge_embeddings.keys())
        edge_embeddings_tensor = torch.stack([
            self.edge_embeddings[key] for key in edge_keys
        ])
        
        # Compute similarities with fixed precision
        with torch.no_grad():
            similarity_scores = F.cosine_similarity(
                query_embedding.unsqueeze(0),
                edge_embeddings_tensor
            )
        
        # Create deterministically ordered pairs
        for idx, (head, tail) in enumerate(edge_keys):
            score = similarity_scores[idx].item()
            if score >= similarity_threshold:
                relation = self.knowledge_graph[head][tail]['relation']
                triple = Triple(head, relation, tail)
                similarities[triple] = score
        
        # Sort by score and alphabetically for ties
        sorted_triples = sorted(
            similarities.items(),
            key=lambda x: (-x[1], x[0].head, x[0].relation, x[0].tail)
        )
        
        return [triple for triple, _ in sorted_triples[:top_k]]

    def expand_subgraph(
        self,
        triples: List[Triple],
        hops: int = 1,
        max_nodes_per_hop: int = 10
    ) -> List[Triple]:
        """
        Expand retrieved subgraph by following connections in a deterministic manner
        
        Args:
            triples: Initial set of triples to expand from
            hops: Number of hops to expand
            max_nodes_per_hop: Maximum number of neighbors to explore per hop
                
        Returns:
            List of expanded Triple objects in deterministic order
        """
        # Use sets for efficient membership testing
        expanded_triples: Set[Triple] = set(triples)
        seen_nodes: Set[str] = {node for triple in triples 
                            for node in (triple.head, triple.tail)}
        
        for _ in range(hops):
            new_triples: Set[Triple] = set()
            
            # Process triples in deterministic order
            for triple in sorted(expanded_triples, key=lambda x: (x.head, x.relation, x.tail)):
                # Process nodes in deterministic order
                for node in sorted([triple.head, triple.tail]):
                    # Get neighboring nodes in deterministic order
                    neighbors = sorted(list(self.knowledge_graph.neighbors(node)))
                    # Apply max_nodes_per_hop limit
                    neighbors = neighbors[:max_nodes_per_hop]
                    
                    # Process neighbors deterministically
                    for neighbor in neighbors:
                        if neighbor not in seen_nodes:
                            # Check outgoing edges
                            if self.knowledge_graph.has_edge(node, neighbor):
                                relation = self.knowledge_graph[node][neighbor]['relation']
                                new_triples.add(Triple(node, relation, neighbor))
                            
                            # Check incoming edges
                            if self.knowledge_graph.has_edge(neighbor, node):
                                relation = self.knowledge_graph[neighbor][node]['relation']
                                new_triples.add(Triple(neighbor, relation, node))
                            
                            seen_nodes.add(neighbor)
                
            expanded_triples.update(new_triples)
        
        # Return sorted list for deterministic ordering
        return sorted(list(expanded_triples), key=lambda x: (x.head, x.relation, x.tail))

    def generate_context(
        self,
        triples: List[Triple],
        format_type: str = 'natural'
    ) -> str:
        """
        Convert retrieved triples into context string with deterministic formatting
        
        Args:
            triples: List of triples to convert
            format_type: Output format ('natural' or 'structured')
                
        Returns:
            Formatted context string
        """
        # Sort triples for deterministic ordering
        sorted_triples = sorted(triples, key=lambda x: (x.head, x.relation, x.tail))
        
        if format_type == 'natural':
            # Create context strings in deterministic order
            context_strings = [
                f"{triple.head} {triple.relation} {triple.tail}."
                for triple in sorted_triples
            ]
            return " ".join(context_strings)
            
        elif format_type == 'structured':
            # Group by subject for organized output
            subject_groups = defaultdict(list)
            for triple in sorted_triples:
                subject_groups[triple.head].append((triple.relation, triple.tail))
            
            # Process groups in deterministic order
            context_parts = []
            for subject in sorted(subject_groups.keys()):
                # Sort predicates for deterministic ordering
                predicates = sorted(subject_groups[subject])
                predicate_str = "; ".join(
                    f"{rel} {obj}" for rel, obj in predicates
                )
                context_parts.append(f"{subject} -> {predicate_str}")
                
            return "\n".join(context_parts)
            
        else:
            raise ValueError(f"Unsupported format type: {format_type}")

def parse_query_with_groq(
    query: str,
    groq_api_key: str,
    seed: int = 42,
    llama_model: str = "llama-3.2-11b-text-preview"
) -> Optional[str]:
    """
    Enhanced query parsing with deterministic settings
    
    Args:
        query: Input query text
        groq_api_key: API key for Groq
        seed: Random seed for reproducibility
        llama_model: Model identifier
    """
    url = "https://api.groq.com/openai/v1/chat/completions"
    
    # Normalize query
    query = ' '.join(query.lower().split())
    
    headers = {
        "Authorization": f"Bearer {groq_api_key}",
        "Content-Type": "application/json"
    }
    
    # Enhanced system message for deterministic behavior
    system_message = """You are a deterministic analytical assistant.
    Process all inputs consistently using these rules:
    1. Always use the same formatting and structure
    2. Sort lists and elements alphabetically
    3. Use consistent terminology
    4. Maintain fixed decimal precision
    5. Follow a fixed reasoning pattern
    6. Avoid any randomization or variation in responses
    """
    
    payload = {
        "model": llama_model,
        "messages": [
            {"role": "system", "content": system_message},
            {"role": "user", "content": query}
        ],
        "temperature": 0,  # Zero temperature for maximum determinism
        "top_p": 1,
        "frequency_penalty": 0,
        "presence_penalty": 0,
        "max_tokens": 500,
        "seed": seed,
        "stream": False
    }
    
    try:
        response = requests.post(url, headers=headers, json=payload, timeout=30)
        response.raise_for_status()
        return response.json()['choices'][0]['message']['content']
    except Exception as e:
        print(f"Error in API request: {e}")
        return None

def createQuery(graph: str, question: str) -> str:
    """
    Create a structured query with deterministic formatting
    
    Args:
        graph: Context information
        question: User question
    """
    # Normalize inputs
    graph = " ".join(graph.split())
    question = " ".join(question.split()).rstrip("?") + "?"
    
    # Enhanced prompt for deterministic responses
    query = f"""
        Context Information:
        {graph}

        Question: {question}

        Output Requirements:
        1. Format: Return a deterministically ordered list of lists
        2. Structure: [["entity1", "entity2", "entity3"], ["entity4", "entity5", "entity6"]]
        3. Rules:
           - Sort entities alphabetically within each inner list
           - Use exactly 3 entities per inner list
           - Maintain consistent terminology
           - Use fixed patterns for similar concepts
           - Sort inner lists by their first entity
        4. Entity Guidelines:
           - Use canonical forms for all entities
           - Maintain consistent capitalization
           - Use fixed terminology for similar concepts
           - Round all numbers to 2 decimal places
        
        Return only the structured list without additional text.
    """
    return query

def demonstrate_rag(query, seed):
    """Example usage of the KnowledgeGraphRAG system"""
    try:
        # Normalize input query
        query = " ".join(query.split()).lower().rstrip("?") + "?"
        
        # Initialize system with fixed random seed
        random.seed(seed)
        np.random.seed(42)
        torch.manual_seed(42)
        os.environ['PYTHONHASHSEED'] = str(seed)
        if torch.cuda.is_available():
            torch.cuda.manual_seed_all(42)

        # Initialize system
        rag = KnowledgeGraphRAG()
        
        # Add sample knowledge
        '''
        sample_triples = [
            ("Einstein", "developed", "Theory of Relativity"),
            ("Theory of Relativity", "describes", "Spacetime"),
            ("Einstein", "won", "Nobel Prize"),
            ("Nobel Prize", "awarded in", "1921"),
            ("Einstein", "worked at", "Patent Office"),
            ("Patent Office", "located in", "Bern"),
            ("Einstein", "born in", "Ulm"),
            ("Ulm", "located in", "Germany")
        ]
        '''

        sample_triples = sorted([
            ("Reliance Industries", "affected by", "Heavy Rainfall"),
            ("Heavy Rainfall", "impacts", "Reliance Oil and Gas Production"),
            ("Monsoon Season", "affects", "Reliance Supply Chain"),
            ("Reliance Industries", "implements", "Flood Protection Measures"),
            ("Reliance Industries", "prepares for", "Adverse Weather Events"),
            ("Reliance Industries", "mitigates risk of", "Flooding"),
            ("Reliance Industries", "monitors", "Rainfall Data for Planning"),
            ("Monsoon Season", "influences", "Reliance Retail Distribution"),
            ("Reliance Industries", "developed", "Climate Adaptation Strategies"),
            ("Reliance Industries", "adapts logistics during", "Monsoon Season"),
            ("Reliance Industries", "risk management includes", "Weather Data Analysis"),
            ("Extreme Weather", "affects", "Reliance Refinery Operations"),
            ("Reliance Industries", "has contingency plans for", "Monsoon Disruptions"),
            ("Reliance Industries", "prepares for", "Cyclones and Heavy Rains"),
            ("Reliance Petrochemical Facilities", "protected against", "Flooding"),
            ("Rainfall Patterns", "impact", "Reliance Agricultural Supply Chain"),
            ("Reliance Industries", "adjusts", "Production During Heavy Rainfall"),
            ("Reliance Industries", "invests in", "Weather-Resilient Infrastructure"),
            ("Reliance Retail", "affected by", "Monsoon Delays"),
            ("Monsoon Delays", "disrupt", "Reliance Industries Supply"),
            ("Reliance Industries", "ensures supply continuity during", "Extreme Weather"),
            ("Flooding", "affects", "Reliance Petrochemical Production"),
            ("Reliance Industries", "adapted infrastructure for", "High Rainfall Events"),
            ("Reliance Industries", "analyzes", "Rainfall Data for Operational Planning"),
            ("Heavy Rainfall", "impacts", "Reliance's Transportation Logistics"),
            ("Reliance Industries", "manages", "Risks Associated with Extreme Weather"),
            ("Cyclones", "pose risk to", "Reliance's Coastal Operations"),
            ("Reliance Industries", "assesses", "Impact of Rainfall on Renewable Energy Operations"),
            ("Weather Conditions", "impact", "Reliance Refinery Transport"),
            ("Reliance Industries", "uses", "Real-Time Weather Monitoring Systems"),
            ("Reliance Industries", "monsoon preparedness plan includes", "Supply Chain Adjustments"),
            ("Reliance Industries", "partnerships for", "Improving Weather Data Accuracy"),
            ("Reliance Industries", "operational planning incorporates", "Seasonal Rainfall Patterns"),
            ("Rainfall", "affects", "Reliance’s Agricultural Commodity Supply"),
            ("Reliance Industries", "works with", "Local Authorities for Flood Management"),
            ("Reliance Industries", "adapts to", "Climate-Related Risks"),
            ("Reliance Industries", "invests in", "Green Energy Projects to Combat Climate Change"),
            ("Climate Change", "influences", "Reliance Industries' Long-Term Strategy")
        ])

        #Create a graph
        for head, relation, tail in sample_triples:
            rag.add_triple(head, relation, tail)
        
        # Retrieve and expand relevant triples
        relevant_triples = rag.retrieve_relevant_subgraph(query, top_k=3)
        expanded_triples = rag.expand_subgraph(relevant_triples, hops=1)

        # Sort triples for consistent output
        expanded_triples.sort(key=lambda x: (x.head, x.relation, x.tail))

        # Generate both natural and structured context
        natural_context = rag.generate_context(expanded_triples, format_type='natural')
        structured_context = rag.generate_context(expanded_triples, format_type='structured')
        
        return {
            'natural_context': natural_context,
            'structured_context': structured_context
        }
        
    except Exception as e:
        print(f"Error in demonstration: {e}")
        return None

if __name__ == "__main__":
    user_query = "What are Einstein's scientific achievements?"
    user_query = "How does winter season affect Reliance's supply chain?"
    seed = 42

    results = demonstrate_rag(user_query, seed)
    if results:
        print(f"\nQuery: {user_query}")
        print("\nNatural Language Context:")
        print(results['natural_context'])
        print("\nStructured Context:")
        print(results['structured_context'])
        query = createQuery(results['structured_context'], user_query)
        print(f"\n Input to LLM : {query}")
        ans = parse_query_with_groq(query, groq_api_key, seed)
        print(f"\n{ans}")
